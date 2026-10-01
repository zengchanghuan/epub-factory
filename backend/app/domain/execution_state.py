"""Pure execution-liveness records. Recovery callers must own the execution lease."""
from __future__ import annotations

from .dispatch_intent import dispatch_identity, timestamp


EXECUTION_FIELDS = ("execution_id", "job_id", "attempt_id", "owner", "state", "heartbeat_at", "recoveries")
EXHAUSTED_CODE = "WORKER_RECOVERY_EXHAUSTED"


def bounded_integer(value, *, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def execution_identity(job):
    return str((job.translation_stats or {}).get("attempt_id") or "")


def migrates_legacy_identity(job, attempt_id):
    return ((job.enable_translation and attempt_id == f"translation-{job.id}")
            or (getattr(job, "enable_precision_polish", False) and attempt_id == f"polish-{job.id}"))


def running_record(job_id, attempt_id, owner, *, now=None, recoveries=0):
    if not isinstance(attempt_id, str) or not isinstance(owner, str) or not owner.strip():
        raise ValueError("Execution requires an explicit attempt string and nonempty owner")
    return {"execution_id": dispatch_identity(job_id, attempt_id), "job_id": job_id,
            "attempt_id": attempt_id, "owner": owner, "state": "running",
            "heartbeat_at": timestamp(now), "recoveries": recoveries}


def legacy_record(job):
    attempt_id = execution_identity(job)
    return {"execution_id": dispatch_identity(job.id, attempt_id), "job_id": job.id,
            "attempt_id": attempt_id, "owner": "", "state": "running",
            "heartbeat_at": timestamp(job.updated_at), "recoveries": 0, "legacy": True}


def recovery_outbox(job_id, attempt_id, *, exhausted, now=None, existing=None):
    at = timestamp(now)
    record = dict(existing or {})
    record.update({"dispatch_id": dispatch_identity(job_id, attempt_id), "job_id": job_id,
                   "attempt_id": attempt_id, "status": "obsolete" if exhausted else "pending",
                   "attempts": record.get("attempts", 0), "created_at": record.get("created_at", at),
                   "updated_at": at, "next_attempt_at": at, "lease_token": "", "lease_expires_at": 0.0,
                   "last_error": EXHAUSTED_CODE if exhausted else ""})
    return record


def recovery_values(job, *, exhausted, now):
    if not exhausted:
        return {"status": "pending", "message": "执行器失联，已自动恢复排队（保留已完成进度）", "updated_at": now}
    stats = {**dict(job.translation_stats or {}), "live": False, "deliverable": False}
    if getattr(job, "enable_precision_polish", False):
        stats["precision_polish"] = {
            **dict(stats.get("precision_polish") or {}),
            "status": "failed", "reason": "worker_recovery_exhausted",
            "validation_passed": False, "refund_required": not job.is_test_order,
        }
    if job.enable_translation or "qa_report" in stats:
        from .translation_qa_service import attach_translation_qa_report
        # No output path: invalidate presentation state without reading artifacts.
        stats = attach_translation_qa_report(stats, error_code=EXHAUSTED_CODE)
    return {"status": "failed", "message": "执行器多次失联，已停止自动恢复；请手动重试或联系管理员。",
            "error_code": EXHAUSTED_CODE, "output_path": None, "updated_at": now,
            "translation_stats": stats}


def unstarted_due(intent, *, now, grace_seconds):
    exponent = min(max(int(intent["attempts"]) - 1, 0), 3)
    return intent["status"] == "sent" and intent["updated_at"] + grace_seconds * (2 ** exponent) <= now
