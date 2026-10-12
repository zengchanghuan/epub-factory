"""Bounded continuation of a paid translation, without creating a user retry.

The cooperative deadline is inspected only before starting a chapter. In-flight
responses and checkpoint writes must be allowed to finish normally.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time

from app.cancellation import JobCancelled


class TranslationSliceExpired(JobCancelled):
    """A drained chapter slice should continue, not cancel the customer's job."""


_deadline = ContextVar("translation_slice_deadline", default=None)
USAGE_COUNTERS = ("api_calls", "prompt_tokens", "completion_tokens", "total_tokens",
                  "timeout_errors", "connection_errors", "retry_attempts")


def cumulative_slice_usage(stats, previous_stats):
    """Add only request usage; book/chunk progress is recomputed, never added."""
    prior = ((previous_stats or {}).get("continuation") or {}).get("usage_totals") or {}
    out = dict(stats)
    for key in USAGE_COUNTERS:
        if key in prior:
            out[key] = (prior.get(key) or 0) + (out.get(key) or 0)
    if prior:
        old_cost, new_cost = prior.get("cost_usd"), out.get("cost_usd")
        if old_cost is None and (prior.get("prompt_tokens") or prior.get("completion_tokens")):
            out["cost_usd"] = None  # Unknown usage costs must not become zero.
        elif new_cost is None and (stats.get("prompt_tokens") or stats.get("completion_tokens")):
            out["cost_usd"] = None
        elif old_cost is not None or new_cost is not None:
            out["cost_usd"] = (old_cost or 0) + (new_cost or 0)
    return out


def _integer(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def slice_seconds():
    soft = _integer("EPUB_BOOK_SOFT_TIME_LIMIT", 7200, 2, 86400)
    reserve = min(120, max(1, soft // 10))
    return min(_integer("EPUB_TRANSLATION_SLICE_SECONDS", 1800, 1, 86400), soft - reserve)


@contextmanager
def translation_slice(enabled=True):
    token = _deadline.set(time.monotonic() + slice_seconds() if enabled else None)
    try:
        yield
    finally:
        _deadline.reset(token)


def slice_expired():
    deadline = _deadline.get()
    return deadline is not None and time.monotonic() >= deadline


def checkpoint_progress(job_id):
    """Count durable useful work; replayed cached chunks and errors add nothing.

Old scopes may be present, but each slice compares the same job at its start
and end. Final review is an extra unit so completing review also advances work.
None means evidence is unavailable; continuation then fails closed.
"""
    path = Path(os.environ.get("EPUB_TRANSLATION_CHECKPOINT_DB", "translation_cache.db"))
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='book_translation_checkpoints'").fetchone():
                return 0
            rows = conn.execute("SELECT item_key,payload FROM book_translation_checkpoints WHERE job_id=?", (job_id,))
            score = 0
            for key, raw in rows:
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict) or not data:
                    continue
                if not key.startswith("chunk:"):
                    score += 1
                    continue
                result = data.get("result")
                if (isinstance(result, dict) and result.get("translated_html")
                        and not result.get("error")):
                    score += 1 + int(data.get("phase") == "final")
            return score
    except (OSError, sqlite3.Error):
        return None


def start_slice_stats(stats, job_id):
    continuation = dict((stats or {}).get("continuation") or {})
    continuation.update(slice_started_at=datetime.now(timezone.utc).isoformat(),
                        slice_start_progress=checkpoint_progress(job_id),
                        slice_seconds=slice_seconds())
    return continuation


def continuation_decision(stats, job_id, reason):
    continuation = dict((stats or {}).get("continuation") or {})
    progress = checkpoint_progress(job_id)
    baseline = continuation.get("slice_start_progress")
    count = int(continuation.get("count") or 0)
    limit = _integer("EPUB_TRANSLATION_MAX_CONTINUATIONS", 48, 0, 96)
    stalled_limit = _integer("EPUB_TRANSLATION_MAX_STALLED_SLICES", 2, 1, 5)
    continuation.update(last_reason=reason, last_progress=progress,
                        last_yield_at=datetime.now(timezone.utc).isoformat())
    continuation["usage_totals"] = {key: (stats or {}).get(key) for key in (*USAGE_COUNTERS, "cost_usd")}
    if progress is None or baseline is None:
        return continuation, "无法确认断点存储状态，已停止自动续跑，请联系客服。"
    stalled = 0 if progress > baseline else int(continuation.get("stalled_slices") or 0) + 1
    continuation["stalled_slices"] = stalled
    if stalled >= stalled_limit:
        return continuation, "任务连续达到运行时限且没有新增已保存进度，已停止自动续跑，请联系客服。"
    if count >= limit:
        return continuation, "任务已达到自动续跑次数上限，已保留译文缓存，请联系客服。"
    continuation["count"] = count + 1
    return continuation, None


def queued_continuation_stats(stats, continuation):
    from copy import deepcopy
    updated = deepcopy(stats or {})
    updated["continuation"] = deepcopy(continuation)
    updated.update(live=False, deliverable=False)
    updated["qa_report"] = {
        "status": "continuing", "summary": "任务已保存进度，正在自动续跑",
        "retryable": False, "free_retry_count": updated.get("free_retry_count", 0),
    }
    return updated
