"""Pure identities and records for durable, at-least-once job dispatch.

Creating an intent is not payment verification. Callers must use an authorized
pending transition, or explicitly request recovery of an already authorized job.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from .translation_attempt import initial_translation_stats


DISPATCH_FIELDS = (
    "dispatch_id", "job_id", "attempt_id", "status", "attempts", "created_at",
    "updated_at", "next_attempt_at", "lease_token", "lease_expires_at", "last_error",
)


def timestamp(value=None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(value)


def dispatch_identity(job_id: str, attempt_id: str) -> str:
    # Length-delimited inputs avoid job/attempt separator collisions.
    payload = f"{len(job_id)}:{job_id}{len(attempt_id)}:{attempt_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_dispatch_intent(job, *, now=None) -> tuple[dict, dict]:
    """Prepare without mutating a Job, so in-memory transitions can be atomic."""
    stats = dict(job.translation_stats or {})
    if (job.enable_translation or getattr(job, "enable_precision_polish", False)) and not stats.get("attempt_id"):
        stats.pop("attempt_id", None)
        stats = initial_translation_stats(stats)
    attempt_id = str(stats.get("attempt_id") or "")
    at = timestamp(now)
    return stats, {
        "dispatch_id": dispatch_identity(job.id, attempt_id),
        "job_id": job.id,
        "attempt_id": attempt_id,
        "status": "pending",
        "attempts": 0,
        "created_at": at,
        "updated_at": at,
        "next_attempt_at": at,
        "lease_token": "",
        "lease_expires_at": 0.0,
        "last_error": "",
    }


def due(record: dict, now: float) -> bool:
    return (
        record["status"] == "pending" and record["next_attempt_at"] <= now
    ) or (
        record["status"] == "publishing" and record["lease_expires_at"] <= now
    )


def completion_values(*, outcome: str, error: str = "", retry_delay_seconds=5, now=None) -> dict:
    if outcome not in {"sent", "obsolete", "retry"}:
        raise ValueError("Unknown dispatch outcome")
    at = timestamp(now)
    return {
        "status": "pending" if outcome == "retry" else outcome,
        "updated_at": at,
        "next_attempt_at": at + max(0.0, float(retry_delay_seconds)) if outcome == "retry" else at,
        "lease_token": "",
        "lease_expires_at": 0.0,
        # Store a bounded diagnostic, never an entire broker traceback/payload.
        "last_error": str(error or "")[:1000],
    }
