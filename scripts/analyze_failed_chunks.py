#!/usr/bin/env python3
"""Analyze archived failed translation chunks."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE_ROOT = ROOT / "backend" / "failed_chunks"


def _load_items(job_id: str, archive_root: Path) -> list[dict]:
    job_dir = archive_root / job_id
    items = []
    for path in sorted(job_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_path"] = str(path)
            items.append(data)
        except Exception:
            continue
    return items


def _error_key(error: str | None) -> str:
    text = (error or "").strip()
    if not text:
        return "unknown"
    text = re.sub(r"\s+", " ", text)
    if "html tag mismatch" in text:
        return "html_tag_mismatch"
    if "untranslated response" in text:
        return "untranslated_response"
    if "json" in text.lower() or "parse" in text.lower():
        return "json_parse"
    if "timeout" in text.lower():
        return "timeout"
    return text.split(";")[0][:80]


def _top_examples(items: list[dict], limit: int = 5) -> list[dict]:
    ranked = sorted(
        items,
        key=lambda x: (int(x.get("retry_count") or 0), int(x.get("latency_ms") or 0)),
        reverse=True,
    )
    out = []
    for item in ranked[:limit]:
        out.append({
            "path": item.get("_path"),
            "chunk_id": item.get("chunk_id"),
            "retry_count": item.get("retry_count"),
            "latency_ms": item.get("latency_ms"),
            "error": item.get("error"),
            "tag_delta": item.get("tag_delta"),
            "source_preview": _preview(item.get("original_html", "")),
            "translated_preview": _preview(item.get("translated_html", "")),
        })
    return out


def _preview(html: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", html or "").strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def analyze(job_id: str, archive_root: Path) -> dict:
    items = _load_items(job_id, archive_root)
    error_counts = Counter(_error_key(item.get("error")) for item in items)
    tag_delta_counts = Counter()
    missing_tag_counts = Counter()
    extra_tag_counts = Counter()
    for item in items:
        for tag, delta in (item.get("tag_delta") or {}).items():
            source_count = int(delta.get("source") or 0)
            translated_count = int(delta.get("translated") or 0)
            tag_delta_counts[tag] += 1
            if source_count > translated_count:
                missing_tag_counts[tag] += source_count - translated_count
            elif translated_count > source_count:
                extra_tag_counts[tag] += translated_count - source_count

    return {
        "job_id": job_id,
        "archive_root": str(archive_root),
        "total_failed_samples": len(items),
        "error_counts": dict(error_counts.most_common()),
        "tag_delta_counts": dict(tag_delta_counts.most_common()),
        "missing_tag_counts": dict(missing_tag_counts.most_common()),
        "extra_tag_counts": dict(extra_tag_counts.most_common()),
        "avg_retry_count": round(sum(int(i.get("retry_count") or 0) for i in items) / len(items), 2) if items else 0,
        "max_latency_ms": max((int(i.get("latency_ms") or 0) for i in items), default=0),
        "top_examples": _top_examples(items),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--archive-root", default=str(DEFAULT_ARCHIVE_ROOT))
    args = parser.parse_args()
    report = analyze(args.job_id, Path(args.archive_root))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
