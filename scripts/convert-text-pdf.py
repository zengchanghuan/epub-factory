#!/usr/bin/env python3
"""Local text-PDF converter. No payment, model, database or network service."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.domain.pdf_conversion import convert_text_pdf, PdfConversionError


def main():
    parser = argparse.ArgumentParser(description="保留原文与原图的本地 PDF → EPUB；不做 OCR 或翻译。")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path, help="必须是不存在的 .epub 文件；父目录须已存在")
    parser.add_argument("--epubcheck-jar", type=Path)
    args = parser.parse_args()
    try:
        report = convert_text_pdf(args.source, args.output, epubcheck_jar=args.epubcheck_jar)
    except PdfConversionError as exc:
        print(json.dumps({"status": "failed", "reason": exc.reason, "message": str(exc)}, ensure_ascii=False))
        return 1
    # No manuscript text, absolute source/output paths or image bytes in stdout.
    fields = ("schema_version", "source_sha256", "output_sha256", "page_count", "normalized_characters",
              "image_assets", "image_placements", "toc_entries", "paragraph_count", "warnings",
              "requires_review", "eligible_for_payment", "validation_passed", "epubcheck_warnings")
    print(json.dumps({key: report[key] for key in fields}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
