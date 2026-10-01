"""Consume durable job-dispatch intents with at-least-once broker publication.

Only authorized payment/create/retry boundaries may create an intent. This
module never infers an entitlement or creates one from a pending job status.
An ambiguous publish/ack crash retains its lease, allowing a later retry; the
executor's attempt fencing and execution lease handle duplicate deliveries.
"""
from __future__ import annotations

import logging

from .translation_attempt import attempt_id_from_stats

logger = logging.getLogger("epub_factory")

# Finite engineering limits, not throughput or exactly-once guarantees.
MAX_BATCH_SIZE = 100
DISPATCH_LEASE_SECONDS = 60
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300


def _retry_delay(attempts) -> int:
    try:
        exponent = min(max(int(attempts) - 1, 0), 6)
    except (TypeError, ValueError, OverflowError):
        exponent = 0
    return min(RETRY_BASE_SECONDS * (2 ** exponent), RETRY_MAX_SECONDS)


def _error(exc, operation):
    # Provider/DB messages and tracebacks can contain URLs, credentials or
    # manuscript data. Persist/log only a fixed operation and exception type.
    return f"{operation} unavailable ({type(exc).__name__[:80]})"


def dispatch_pending(store, publisher, *, job_id=None, limit=20, now=None) -> dict[str, int]:
    """Publish at most ``limit`` existing intents; never mutate job status.

    ``publisher(job_id, expected_attempt_id)`` must raise on failed publication.
    ``published`` counts successful publisher returns; ``sent`` counts durable
    acknowledgements (including jobs already running). Errors are explicit even
    if publication succeeded but acknowledgement did not. No store exception is
    reported as an acknowledged success; a failed claim ends this bounded pass.
    """
    counts = {key: 0 for key in ("claimed", "published", "sent", "obsolete", "retry", "errors")}
    bound = min(max(int(limit), 0), MAX_BATCH_SIZE)

    def finish(record, outcome, *, error=""):
        try:
            acknowledged = store.finish_dispatch(
                record["dispatch_id"], record["lease_token"], outcome=outcome,
                error=error, retry_delay_seconds=_retry_delay(record.get("attempts")), now=now,
            )
        except Exception as exc:
            counts["errors"] += 1
            logger.warning(_error(exc, "Job dispatch acknowledgement"))
            return
        if not acknowledged:
            counts["errors"] += 1
            logger.warning("Job dispatch acknowledgement rejected; lease may have changed")
            return
        counts[outcome] += 1

    for _ in range(bound):
        try:
            record = store.claim_dispatch(job_id=job_id, lease_seconds=DISPATCH_LEASE_SECONDS, now=now)
        except Exception as exc:
            counts["errors"] += 1
            logger.warning(_error(exc, "Job dispatch claim"))
            break
        if record is None:
            break
        counts["claimed"] += 1
        try:
            job = store.get(record["job_id"])
            status = getattr(job, "status", None)
            status = getattr(status, "value", status)
            current_attempt = attempt_id_from_stats(getattr(job, "translation_stats", None))
            if job is None or current_attempt != record["attempt_id"] or status not in {"pending", "running"}:
                finish(record, "obsolete")
                continue
            if status == "running":
                # Lost running workers are a separate recovery policy. Do not
                # resubmit or infer failure from this publisher's observation.
                finish(record, "sent")
                continue
        except Exception as exc:
            counts["errors"] += 1
            finish(record, "retry", error=_error(exc, "Job dispatch state read"))
            continue
        try:
            publisher(record["job_id"], record["attempt_id"])
        except Exception as exc:
            counts["errors"] += 1
            finish(record, "retry", error=_error(exc, "Job dispatch publish"))
            continue
        counts["published"] += 1
        # If this acknowledgement fails, do not make a second state change:
        # leave the claim lease for a later at-least-once publication attempt.
        finish(record, "sent")
    return counts
