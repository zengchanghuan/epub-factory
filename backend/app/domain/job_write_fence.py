"""Shared attempt/owner write policy; stores apply it under the parent lock."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timezone

from app.cancellation import JobCancelled


class JobWriteConflict(JobCancelled):
    """The captured executor no longer owns this job; stop all old writes."""


@dataclass(frozen=True)
class JobWriteFence:
    job_id: str
    attempt_id: str
    execution_owner: str


_WRITE_FENCE = ContextVar("job_write_fence", default=None)


def current_job_write_fence():
    return _WRITE_FENCE.get()


@contextmanager
def job_write_scope(job_id: str, attempt_id: str, execution_owner: str):
    if not isinstance(attempt_id, str) or not isinstance(execution_owner, str) or not execution_owner.strip():
        raise ValueError("A write scope requires an explicit attempt string and executor owner")
    fence = JobWriteFence(job_id, attempt_id, execution_owner)
    token = _WRITE_FENCE.set(fence)
    try:
        yield fence
    finally:
        _WRITE_FENCE.reset(token)


def utc_datetime(value):
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def reject_write():
    if current_job_write_fence() is not None:
        raise JobWriteConflict("任务状态、翻译尝试或执行器归属已变化，旧执行器已停止写入")
    return False


def translation_stats_for_status(job, status, patch):
    """Cancel paid precision work using the latest locked progress, not a UI snapshot."""
    current = dict(job.translation_stats or {})
    if getattr(status, "value", status) == "cancelled" and job.enable_precision_polish:
        # A cancellation changes disposition only. Old runner/UI dictionaries
        # must not roll back the current quote, counters or checkpoint metadata.
        return {**current, "precision_polish": {
            **dict(current.get("precision_polish") or {}),
            "status": "cancelled", "reason": "cancelled", "validation_passed": False,
            "refund_required": not job.is_test_order,
        }}
    if patch is None:
        return None
    if isinstance(patch, dict):
        return {**current, **patch}
    return patch


def check_job_write(job, job_id, *, execution=None, expected_attempt_id=None,
                    expected_statuses=None, expected_updated_at=None, translation_stats=None):
    """Return false for rejected commands; scoped executor conflicts always raise."""
    fence = current_job_write_fence()
    if expected_attempt_id is not None and not isinstance(expected_attempt_id, str):
        raise ValueError("expected_attempt_id must be a string or None")
    if job is None:
        return reject_write()
    attempt = str((job.translation_stats or {}).get("attempt_id") or "")
    status = getattr(job.status, "value", job.status)
    if expected_attempt_id is not None and expected_attempt_id != attempt:
        return reject_write()
    if expected_statuses is not None:
        statuses = {getattr(item, "value", item) for item in expected_statuses}
        if status not in statuses:
            return reject_write()
    if expected_updated_at is not None and utc_datetime(job.updated_at) != utc_datetime(expected_updated_at):
        return reject_write()
    if fence is not None:
        if (job_id != fence.job_id or attempt != fence.attempt_id or status != "running"
                or (expected_attempt_id is not None and expected_attempt_id != fence.attempt_id)
                or not execution or execution.get("state") != "running"
                or execution.get("owner") != fence.execution_owner
                or execution.get("attempt_id") != fence.attempt_id):
            return reject_write()
        if isinstance(translation_stats, dict) and "attempt_id" in translation_stats:
            if translation_stats["attempt_id"] != fence.attempt_id:
                return reject_write()
    return True
