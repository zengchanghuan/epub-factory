"""Local archive for failed translation chunks.

The archive is intentionally file-based and outside git. It stores final failed
chunks, chunks with an explicit error, and chunks that required repeated retry
attempts so we can inspect LLM formatting failures without querying SQLite by
hand.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag


logger = logging.getLogger("epub.failed_chunks")

BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}


def archive_root() -> Path:
    configured = os.environ.get("EPUB_FAILED_CHUNK_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "failed_chunks"


def _enabled() -> bool:
    return os.environ.get("EPUB_FAILED_CHUNK_ARCHIVE", "1").lower() not in {"0", "false", "no", "off"}


def _safe_name(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text[:120] or fallback


def _status_value(status: Any) -> str:
    return getattr(status, "value", str(status or ""))


def _tag_counter(html: str) -> dict[str, int]:
    soup = BeautifulSoup(html or "", "html.parser")
    counter = Counter(
        tag.name
        for tag in soup.find_all(True)
        if isinstance(tag, Tag) and tag.name not in BLOCK_TAGS
    )
    return dict(sorted(counter.items()))


def _tag_delta(source_html: str, translated_html: str) -> dict[str, dict[str, int]]:
    source = _tag_counter(source_html)
    translated = _tag_counter(translated_html)
    out: dict[str, dict[str, int]] = {}
    for name in sorted(set(source) | set(translated)):
        if source.get(name, 0) != translated.get(name, 0):
            out[name] = {
                "source": source.get(name, 0),
                "translated": translated.get(name, 0),
            }
    return out


def _text_len(html: str) -> int:
    return len(BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True))


def _should_archive(chunk: Any, status: Any) -> bool:
    status_text = _status_value(status)
    error = str(getattr(chunk, "error", "") or "")
    retry_count = int(getattr(chunk, "retry_count", 0) or 0)
    if status_text == "failed" or error:
        return True
    min_retries = int(os.environ.get("EPUB_FAILED_CHUNK_MIN_RETRIES", "2") or "2")
    return retry_count >= min_retries


def archive_failed_chunk(*, job_id: str, chapter_id: str, chunk: Any, status: Any) -> Path | None:
    """Persist a failed chunk snapshot and return its path.

    This function must never break the translation pipeline; callers can invoke
    it after writing job_chunks.
    """
    if not _enabled() or not _should_archive(chunk, status):
        return None

    original_html = str(getattr(chunk, "original_html", "") or "")
    translated_html = str(getattr(chunk, "translated_html", "") or "")
    chunk_id = str(getattr(chunk, "chunk_id", "") or "chunk")
    sequence = int(getattr(chunk, "sequence", 0) or 0)
    payload = {
        "schema_version": 1,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "chapter_id": chapter_id,
        "chunk_id": chunk_id,
        "sequence": sequence,
        "locator": str(getattr(chunk, "locator", "") or ""),
        "status": _status_value(status),
        "retry_count": int(getattr(chunk, "retry_count", 0) or 0),
        "error": getattr(chunk, "error", None),
        "error_type": getattr(chunk, "error_type", None),
        "model": getattr(chunk, "model", None),
        "base_url": getattr(chunk, "base_url", None),
        "prompt_tokens": int(getattr(chunk, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(chunk, "completion_tokens", 0) or 0),
        "latency_ms": int(getattr(chunk, "latency_ms", 0) or 0),
        "source_text_len": _text_len(original_html),
        "translated_text_len": _text_len(translated_html),
        "source_tags": _tag_counter(original_html),
        "translated_tags": _tag_counter(translated_html),
        "tag_delta": _tag_delta(original_html, translated_html),
        "audit_json": getattr(chunk, "audit_json", {}) or {},
        "original_html": original_html,
        "translated_html": translated_html,
    }

    try:
        root = archive_root() / _safe_name(job_id)
        root.mkdir(parents=True, exist_ok=True)
        name = f"{sequence:06d}-{_safe_name(chapter_id)}-{_safe_name(chunk_id)}.json"
        path = root / name
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
        return path
    except Exception:
        logger.warning("failed to archive translation chunk", exc_info=True)
        return None
