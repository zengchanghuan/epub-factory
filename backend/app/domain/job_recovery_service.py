"""Recover lost executions, not slow/live executions or unpaid orders.

Heartbeat staleness only selects candidates. Holding the SAME execution lease
is mandatory before the store atomically rechecks identity, heartbeat and cap.
Publication remains the durable outbox's responsibility, independent of Celery.
"""
from __future__ import annotations

import logging
import math
import os
from contextlib import ExitStack, contextmanager

from .dispatch_intent import timestamp
from .translation_attempt import attempt_id_from_stats
from ..infra.execution_lease import execution_identity, execution_lease

logger = logging.getLogger("epub_factory")


@contextmanager
def _recovery_lease(job, *, legacy=False):
    identity = execution_identity(job)
    identities = [identity]
    # Pre-R7 translation could persist a generated attempt AFTER acquiring the
    # old "conversion" lock. During rolling upgrades both identities must be
    # unowned before a legacy job is considered lost.
    if job.enable_translation and (legacy or not attempt_id_from_stats(job.translation_stats)) and identity != "conversion":
        identities.append("conversion")
    with ExitStack() as stack:
        leases = []
        for key in identities:
            lease = stack.enter_context(execution_lease(job.id, key))
            if lease is None:
                yield None
                return
            leases.append(lease)
        for lease in leases:
            lease.assert_owned()
        yield leases[0]


def recovery_config():
    """Engineering defaults, not a promise that a dead job resumes instantly."""
    stale = float(os.environ.get("JOB_RECOVERY_STALE_SECONDS", "600"))
    maximum = int(os.environ.get("JOB_RECOVERY_MAX_ATTEMPTS", "2"))
    queue_grace = int(os.environ.get("JOB_RECOVERY_QUEUE_GRACE_SECONDS", "3600"))
    if not math.isfinite(stale) or not 60 <= stale <= 86400:
        raise ValueError("JOB_RECOVERY_STALE_SECONDS must be between 60 and 86400")
    if not 0 <= maximum <= 10:
        raise ValueError("JOB_RECOVERY_MAX_ATTEMPTS must be between 0 and 10")
    if not 600 <= queue_grace <= 86400:
        raise ValueError("JOB_RECOVERY_QUEUE_GRACE_SECONDS must be between 600 and 86400")
    return stale, maximum, queue_grace


def recover_lost_executions(store, *, now=None, stale_seconds=600, max_recoveries=2, limit=20,
                            queue_grace_seconds=3600):
    at = timestamp(now)
    if not math.isfinite(at) or not math.isfinite(float(stale_seconds)) or float(stale_seconds) < 0:
        raise ValueError("Recovery timestamps must be finite and staleness nonnegative")
    cutoff = at - float(stale_seconds)
    counts = dict(scanned=0, recovered=0, exhausted=0, busy=0, unchanged=0, errors=0, queued_rearmed=0)
    candidates = store.list_stale_executions(stale_before=cutoff, limit=limit)
    for candidate in candidates:
        counts["scanned"] += 1
        try:
            job = store.get(candidate["job_id"])
            attempt = candidate["attempt_id"]
            if job is None or attempt_id_from_stats(job.translation_stats) != attempt:
                counts["unchanged"] += 1
                continue
            with _recovery_lease(job, legacy=candidate.get("legacy", False)) as lease:
                if lease is None:
                    counts["busy"] += 1
                    continue
                lease.assert_owned()
                outcome = store.recover_execution(
                    job.id, attempt, candidate["owner"], stale_before=cutoff,
                    now=at, max_recoveries=max_recoveries,
                )
                counts[outcome] += 1
        except Exception as exc:
            # A failed lease or database check never authorizes recovery. No
            # book text, connection string or exception payload enters logs.
            counts["errors"] += 1
            logger.warning("Execution recovery unavailable (%s)", type(exc).__name__[:80])
    # A child can die before admission; the broker may ACK that delivery while
    # SQL still says pending. A long, exponentially backed-off redelivery is
    # safe under the same lease and begin CAS. It is NOT an execution failure:
    # genuine queue waiting must never consume the poison-book recovery cap.
    for intent in store.list_unstarted_dispatches(now=at, grace_seconds=queue_grace_seconds, limit=limit):
        try:
            job = store.get(intent["job_id"])
            if job is None or attempt_id_from_stats(job.translation_stats) != intent["attempt_id"]:
                continue
            with _recovery_lease(job) as lease:
                if lease is None:
                    counts["busy"] += 1
                    continue
                lease.assert_owned()
                if store.rearm_unstarted_dispatch(job.id, intent["attempt_id"], sent_at=intent["updated_at"],
                                                  now=at, grace_seconds=queue_grace_seconds):
                    counts["queued_rearmed"] += 1
        except Exception as exc:
            counts["errors"] += 1
            logger.warning("Unstarted delivery recovery unavailable (%s)", type(exc).__name__[:80])
    return counts
