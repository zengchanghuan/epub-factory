"""Bind acceptance evidence to the exact bytes published or quarantined."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path


def receipt(path, attempt_id):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size,
            "attempt_id": attempt_id, "accepted_at": datetime.now(timezone.utc).isoformat()}


def matches(path, manifest):
    try:
        path = Path(path)
        if not path.is_file():
            return False
        if manifest is None:  # Already-delivered legacy artifacts have no receipt.
            return True
        if (not isinstance(manifest, dict) or type(manifest.get("bytes")) is not int
                or manifest["bytes"] < 0 or not isinstance(manifest.get("sha256"), str)):
            return False
        return (path.stat().st_size == manifest["bytes"]
                and receipt(path, manifest.get("attempt_id"))["sha256"] == manifest["sha256"])
    except (OSError, KeyError, TypeError, ValueError):
        return False
