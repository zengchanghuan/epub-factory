#!/usr/bin/env python3
"""Private local diagnostic only: does not enable PDF upload, quote or translation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app.domain.pdf_text_preflight import (  # noqa: E402
    PdfPreflightError,
    inspect_text_pdf,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="A local PDF; original bytes are never changed")
    parser.add_argument("--report", type=Path, help="Optional new JSON file (0600); refuses overwrite")
    args = parser.parse_args(argv)
    try:
        report = inspect_text_pdf(args.source)
    except PdfPreflightError as exc:
        print(json.dumps({"status": "rejected", "reason": exc.reason, "message": str(exc),
                          "eligible_for_payment": False}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print("Local PDF inspection cancelled.", file=sys.stderr)
        return 130

    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report is not None:
        try:
            descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
        except OSError:
            print("Cannot create a new private report; existing files were not overwritten.", file=sys.stderr)
            return 3
    else:
        print(payload, end="")
    # A successful inspection is not permission to accept a paid PDF order.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
