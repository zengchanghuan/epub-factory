#!/usr/bin/env python3
"""Archive failed chunks for an existing job.

Usage:
  python scripts/archive_failed_chunks_for_job.py 6104ad1bf92b
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.domain.chapter_translation_service import ChunkResult  # noqa: E402
from app.domain.failed_chunk_archive import archive_failed_chunk, archive_root  # noqa: E402
from app.domain.manifest_service import build_manifest  # noqa: E402
from app.models import ChunkStatus  # noqa: E402
from app.storage import job_store  # noqa: E402


def _status_value(value) -> str:
    return getattr(value, "value", str(value or ""))


def _manifest_chunk_map(job) -> dict[str, dict]:
    try:
        manifest = build_manifest(job.input_path, job.id)
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for chapter in manifest.get("chapters", []):
        for spec in chapter.get("chunks") or []:
            if spec.get("chunk_id"):
                enriched = dict(spec)
                enriched["chapter_id"] = chapter.get("chapter_id")
                enriched["file_path"] = chapter.get("file_path")
                out[spec["chunk_id"]] = enriched
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--include-nonfailed", action="store_true")
    args = parser.parse_args()

    job = job_store.get(args.job_id)
    if not job:
        print(json.dumps({"ok": False, "error": "job not found", "job_id": args.job_id}, ensure_ascii=False))
        return 1

    specs = _manifest_chunk_map(job)
    chunks = []
    for chunk in job_store.list_chunks(args.job_id):
        status = _status_value(getattr(chunk, "status", ""))
        if not args.include_nonfailed and status != ChunkStatus.failed.value and not getattr(chunk, "error_message", None):
            continue
        chunks.append(chunk)

    archived = []
    for chunk in chunks:
        spec = specs.get(getattr(chunk, "chunk_id", ""), {})
        original_html = spec.get("html") or getattr(chunk, "source_text", "") or ""
        translated_html = getattr(chunk, "translated_text", "") or ""
        cr = ChunkResult(
            chunk_id=getattr(chunk, "chunk_id", ""),
            sequence=int(getattr(chunk, "sequence", 0) or 0),
            locator=getattr(chunk, "locator", "") or spec.get("locator", ""),
            original_html=original_html,
            translated_html=translated_html,
            cached=bool(getattr(chunk, "cached", False)),
            error=getattr(chunk, "error_message", None),
            model=getattr(chunk, "model", None),
            base_url=getattr(chunk, "base_url", None),
            prompt_tokens=int(getattr(chunk, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(chunk, "completion_tokens", 0) or 0),
            latency_ms=int(getattr(chunk, "latency_ms", 0) or 0),
            retry_count=int(getattr(chunk, "retry_count", 0) or 0),
            audit_json=getattr(chunk, "audit_json", {}) or {},
        )
        path = archive_failed_chunk(
            job_id=args.job_id,
            chapter_id=getattr(chunk, "chapter_id", "") or spec.get("chapter_id", ""),
            chunk=cr,
            status=getattr(chunk, "status", ChunkStatus.failed),
        )
        if path:
            archived.append(str(path))

    print(json.dumps({
        "ok": True,
        "job_id": args.job_id,
        "archive_root": str(archive_root()),
        "failed_or_error_chunks": len(chunks),
        "archived": len(archived),
        "sample_paths": archived[:5],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
