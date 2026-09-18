"""Book-scoped, versioned resume data in the existing persistent cache DB.

Unlike a best-effort translation-cache hit, a checkpoint is bound to the exact
source, settings and book context. Attempt IDs are deliberately excluded for
reuse/verified retries; fresh attempts are isolated. Consumers revalidate text
and markup with the current QA rules before accepting persisted results.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

CHECKPOINT_VERSION = "book-resume-v1"


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def book_resume_key(job, manifest: dict) -> str:
    """Invalidate on source/locator, preprocessing or user/provider changes."""
    settings = {name: getattr(job, name, None) for name in (
        "output_mode", "device", "traditional_variant", "lexicon_domains",
        "enable_proper_noun", "target_lang", "bilingual", "translation_quality",
        "temperature", "translation_strategy", "glossary",
    )}
    settings["model"] = (getattr(job, "translation_model", None)
                         or os.environ.get("EPUB_DEFAULT_TRANSLATION_MODEL")
                         or os.environ.get("OPENAI_MODEL", "deepseek-flash"))
    settings["provider"] = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    settings["preflight"] = (getattr(job, "translation_stats", None) or {}).get("translation_preflight")
    # A fresh user attempt must not reuse an earlier attempt's checkpoint, but
    # a worker restart within that same fresh attempt can still resume safely.
    if getattr(job, "cache_policy", "reuse") == "fresh":
        settings["fresh_attempt"] = (getattr(job, "translation_stats", None) or {}).get("attempt_id") or uuid.uuid4().hex
    settings["profiler_config"] = {key: os.environ.get(key) for key in (
        "EPUB_BOOK_PROFILER_ENABLED", "EPUB_BOOK_PROFILER_MODEL", "EPUB_BOOK_PROFILER_MAX_CHARS",
    )}
    settings["llm_configured"] = bool(os.environ.get("OPENAI_API_KEY", "").strip()
                                     not in {"", "dummy"})
    source_sha = None
    source_path = Path(getattr(job, "input_path", "") or "")
    if source_path.is_file():
        digest = hashlib.sha256()
        with source_path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        source_sha = digest.hexdigest()
    return fingerprint({"version": CHECKPOINT_VERSION, "source_sha256": source_sha, "settings": settings,
                        "chapters": manifest.get("chapters", [])})


class TranslationCheckpoints:
    def __init__(self, job_id: str, scope: str, db_path: str | None = None):
        self.job_id, self.scope = job_id, scope
        self.db_path = db_path or os.environ.get("EPUB_TRANSLATION_CHECKPOINT_DB", "translation_cache.db")
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS book_translation_checkpoints (
                job_id TEXT NOT NULL, scope TEXT NOT NULL, item_key TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (job_id, scope, item_key))""")

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=5)

    def get(self, item_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM book_translation_checkpoints WHERE job_id=? AND scope=? AND item_key=?",
                               (self.job_id, self.scope, item_key)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None

    def put(self, item_key: str, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO book_translation_checkpoints VALUES (?, ?, ?, ?, ?)",
                         (self.job_id, self.scope, item_key, json.dumps(payload, ensure_ascii=False), time.time()))

    def chunks(self) -> dict[str, dict]:
        """One indexed read per book, not one connection/scan per paragraph."""
        with self._connect() as conn:
            rows = conn.execute("SELECT item_key,payload FROM book_translation_checkpoints WHERE job_id=? AND scope=? AND item_key LIKE 'chunk:%'",
                                (self.job_id, self.scope)).fetchall()
        out = {}
        for key, raw in rows:
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    out[key[6:]] = value
            except (ValueError, TypeError):
                continue
        return out
