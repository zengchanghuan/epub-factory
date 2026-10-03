#!/usr/bin/env python3
"""Local PDF structure diagnostics, not a translation or paid-delivery approval.

Default stdout contains statistics only. --report explicitly saves a new private
JSON file containing manuscript text and encoded image streams; do not publish it.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app.domain.pdf_text_ir import PdfIrError, extract_pdf_text_ir  # noqa: E402


def write_private_report(destination, result):
    """Publish a complete private report without replacing any existing name."""
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".pdf-ir-", suffix=".tmp", dir=destination.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(result, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # link is atomic and fails if destination (including a symlink) exists.
        # Unlike replace/rename, it cannot clobber another report created meanwhile.
        os.link(temporary, destination, follow_symlinks=False)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Read-only local PDF original")
    parser.add_argument("--report", type=Path,
                        help="New 0600 private JSON with source text and image data; refuses overwrite")
    args = parser.parse_args(argv)
    try:
        result = extract_pdf_text_ir(args.source)
    except PdfIrError as exc:
        print(json.dumps({"status": "rejected", "reason": exc.reason, "message": str(exc),
                          "eligible_for_payment": False}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print("Local PDF structure inspection cancelled.", file=sys.stderr)
        return 130

    if args.report is not None:
        try:
            write_private_report(args.report, result)
        except (OSError, UnicodeError):
            print("Cannot write a new private IR report; existing files were not overwritten.", file=sys.stderr)
            return 3
        except KeyboardInterrupt:
            print("Local PDF structure inspection cancelled.", file=sys.stderr)
            return 130
    summary = {key: result[key] for key in (
        "schema_version", "source_sha256", "parser_version", "status", "eligible_for_payment",
        "page_count", "total_char_count", "total_nonspace_count", "total_fragment_count",
        "total_encoded_resource_bytes", "flags",
    )}
    summary["unique_image_resources"] = len(result["resources"])
    summary["page_image_resource_references"] = sum(len(page["image_resource_ids"]) for page in result["pages"])
    summary["private_report_written"] = args.report is not None
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
