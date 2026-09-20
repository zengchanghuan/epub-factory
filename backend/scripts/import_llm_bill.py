"""Explicit local import of request-level provider bill amounts (no API calls).

Input: JSON list of ledger_id, response_id, provider hostname, currency, amount.
An original provider bill file is required separately for its audit SHA-256.
Default is validation only. --apply persists matched values idempotently.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.infra.llm_usage_ledger import get_ledger
from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / '.env', override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matches", type=Path)
    parser.add_argument("--source-bill", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rows = json.loads(args.matches.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("matches must be a nonempty JSON list")
    digest = hashlib.sha256(args.source_bill.read_bytes()).hexdigest()
    ledger = get_ledger()
    seen = set()
    for row in rows:
        identity = (row["provider"], row["response_id"])
        if identity in seen:
            raise ValueError("duplicate provider response in import")
        seen.add(identity)
        ledger.import_bill(row["ledger_id"], response_id=row["response_id"], provider=row["provider"],
                           currency=row["currency"], amount=row["amount"], source_sha256=digest, dry_run=True)
    if args.apply:
        for row in rows:
            ledger.import_bill(row["ledger_id"], response_id=row["response_id"], provider=row["provider"],
                               currency=row["currency"], amount=row["amount"], source_sha256=digest)
    print(json.dumps({"rows": len(rows), "applied": args.apply, "source_sha256": digest}))


if __name__ == "__main__":
    main()
