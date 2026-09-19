"""Terminal in-app notifications and opt-in completion mail scheduling.

Notification failures never change the task's outcome. Private recipient details
and access links belong only in the dedicated email subscription table.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.models import JobNotification, JobStatus, NotificationStatus
from app.storage import job_store
from .completion_email_service import queue_completion_email

logger = logging.getLogger("epub_factory")
CHANNEL_IN_APP = "in_app"
CHANNEL_EMAIL = "email"


def _payload_for_job(job_id: str, status: JobStatus, message: str,
                     error_code: Optional[str] = None,
                     output_path: Optional[str] = None,
                     source_filename: Optional[str] = None) -> Dict[str, Any]:
    safe_message = "任务已完成" if status == JobStatus.success else "任务已取消" if status == JobStatus.cancelled else "任务处理失败，请查看订单状态或联系客服"
    safe_error = error_code if error_code and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", error_code) else None
    return {"job_id": job_id, "status": status.value, "message": safe_message,
            "error_code": safe_error, "completed_at": datetime.now(timezone.utc).isoformat()}


def notify_job_completed(job_id: str, status: JobStatus, message: str,
                         error_code: Optional[str] = None,
                         output_path: Optional[str] = None,
                         source_filename: Optional[str] = None,
                         user_id: Optional[str] = None) -> None:
    payload = _payload_for_job(job_id, status, message, error_code, output_path, source_filename)
    try:
        add_fn = getattr(job_store, "add_notification", None)
        if add_fn:
            add_fn(JobNotification(job_id=job_id, channel=CHANNEL_IN_APP,
                                   status=NotificationStatus.sent, payload=payload, user_id=user_id,
                                   sent_at=datetime.now(timezone.utc)))
    except Exception:
        logger.warning("add in_app notification failed", extra={"job_id": job_id})
    try:
        queue_completion_email(job_id, status)
    except Exception:
        # Reconciliation in the dispatcher repairs a missed completion hook.
        logger.warning("queue completion email failed", extra={"job_id": job_id})
