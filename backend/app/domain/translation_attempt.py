"""Helpers for isolating translation attempts and resetting per-attempt metrics."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def new_attempt_id() -> str:
    return uuid.uuid4().hex


def initial_translation_stats(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the minimal durable identity for a first translation attempt."""
    stats = dict(existing or {})
    stats.setdefault("attempt_id", new_attempt_id())
    stats.setdefault("translation_attempt", 1)
    stats.setdefault("restart_count", 0)
    stats.setdefault("free_retry_count", 0)
    return stats


def restarted_translation_stats(
    previous: dict[str, Any] | None,
    *,
    attempt_id: str,
    started_at: datetime | None = None,
    model: str = "",
    max_free_retries: int = -1,
    action_label: str = "重启翻译",
) -> dict[str, Any]:
    """Build a replacement stats object; no per-attempt counters are inherited."""
    old = dict(previous or {})
    started_at = started_at or datetime.now(timezone.utc)
    translation_attempt = int(old.get("translation_attempt") or 1) + 1
    free_retry_count = int(old.get("free_retry_count") or 0) + 1
    stats: dict[str, Any] = {
        "attempt_id": attempt_id,
        "attempt_started_at": started_at.isoformat(),
        "translation_attempt": translation_attempt,
        "restart_count": int(old.get("restart_count") or 0) + 1,
        "free_retry_count": free_retry_count,
        "model": model or "",
        "total_chunks": 0,
        "chunks_total": 0,
        "manifest_chunks_total": 0,
        "translated_chunks": 0,
        "cached_chunks": 0,
        "failed_chunks": 0,
        "chunks_skipped": 0,
        "retry_attempts": 0,
        "api_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "timeout_errors": 0,
        "connection_errors": 0,
        "api_latency_ms_total": 0,
        "api_latency_ms_max": 0,
        "api_latency_samples": 0,
        "quality_fallback_attempts": 0,
        "text_segment_rescue_attempts": 0,
        "text_segment_rescue_successes": 0,
        "chunk_rescue_attempts": 0,
        "chunk_rescue_successes": 0,
        "retry_budget_exhausted_chunks": 0,
        "failed_chunk_rescue_candidates": 0,
        "failed_chunk_rescue_attempted": 0,
        "failed_chunk_rescue_succeeded": 0,
        "failed_chunk_rescue_failed": 0,
        "image_note_chunks_skipped": 0,
        "image_caption_chunks": 0,
        "reference_note_chunks_skipped": 0,
        "structured_note_chunks": 0,
        "last_error": "",
        "audit_warn_chunks": 0,
        "audit_failed_chunks": 0,
        "audit_flags_count": {},
        "audit_examples": [],
        "artifact_audit": {},
        "delivery_gate_failed": False,
        "deliverable": None,
        "elapsed_seconds": 0,
        "cost_usd": 0,
        "live": False,
    }
    restart_summary = f"{action_label}已排队"
    stats["qa_report"] = {
        "status": "retrying",
        "summary": restart_summary,
        "retryable": False,
        "free_retry_count": free_retry_count,
        "max_free_retries": max_free_retries,
        "translation_attempt": translation_attempt,
        "flags": [],
        "checks": [],
        "score": 0,
    }
    return stats


def attempt_id_from_stats(stats: dict[str, Any] | None) -> str:
    return str((stats or {}).get("attempt_id") or "")
