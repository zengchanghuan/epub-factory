"""
通知系统第一版：任务完成时写站内通知，可选邮件通知。

通知失败不反写任务状态，只记录到 notifications（设计 12.3）。
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.models import JobNotification, JobStatus, NotificationStatus
from app.storage import job_store

logger = logging.getLogger("epub_factory")

CHANNEL_IN_APP = "in_app"
CHANNEL_EMAIL = "email"


def _payload_for_job(
    job_id: str,
    status: JobStatus,
    message: str,
    error_code: Optional[str] = None,
    output_path: Optional[str] = None,
    source_filename: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status.value,
        "message": message,
        "error_code": error_code,
        "output_path": output_path,
        "source_filename": source_filename,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def notify_job_completed(
    job_id: str,
    status: JobStatus,
    message: str,
    error_code: Optional[str] = None,
    output_path: Optional[str] = None,
    source_filename: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """
    任务结束（成功/失败/校验失败）时调用，写站内通知；若开启邮件则尝试发邮件并写记录。
    """
    add_fn = getattr(job_store, "add_notification", None)
    if not add_fn:
        return
    payload = _payload_for_job(
        job_id=job_id,
        status=status,
        message=message,
        error_code=error_code,
        output_path=output_path,
        source_filename=source_filename,
    )
    try:
        in_app = JobNotification(
            job_id=job_id,
            channel=CHANNEL_IN_APP,
            status=NotificationStatus.sent,
            payload=payload,
            user_id=user_id,
            sent_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        add_fn(in_app)
        logger.info("notification added", extra={"job_id": job_id, "channel": CHANNEL_IN_APP})
    except Exception as e:
        logger.warning("add in_app notification failed", extra={"job_id": job_id, "error": str(e)})

    if os.environ.get("NOTIFY_EMAIL_ENABLED", "").lower() not in ("1", "true", "yes"):
        return
    _try_send_email_notification(
        job_id=job_id,
        payload=payload,
        user_id=user_id,
        add_fn=add_fn,
    )


def _try_send_email_notification(
    job_id: str,
    payload: Dict[str, Any],
    user_id: Optional[str],
    add_fn,
) -> None:
    """可选：发送邮件并写入一条 channel=email 的通知记录。"""
    to_addr = os.environ.get("NOTIFY_EMAIL_TO") or (user_id if user_id and "@" in str(user_id) else None)
    if not to_addr:
        return
    now = datetime.now(timezone.utc)
    rec = JobNotification(
        job_id=job_id,
        channel=CHANNEL_EMAIL,
        status=NotificationStatus.pending,
        payload=payload,
        user_id=user_id,
        created_at=now,
    )
    try:
        _send_email(
            to_addr=to_addr,
            subject=f"[EPUB Factory] 任务 {job_id} 已完成",
            body=_email_body(payload),
        )
        rec.status = NotificationStatus.sent
        rec.sent_at = now
    except Exception as e:
        logger.warning("email send failed", extra={"job_id": job_id, "error": str(e)})
        rec.status = NotificationStatus.failed
        rec.error_message = str(e)
    try:
        add_fn(rec)
    except Exception as e:
        logger.warning("add email notification record failed", extra={"job_id": job_id, "error": str(e)})


def _email_body(payload: Dict[str, Any]) -> str:
    status = payload.get("status", "")
    msg = payload.get("message", "")
    err = payload.get("error_code", "")
    return f"任务 {payload.get('job_id', '')} 状态: {status}\n\n{msg}\n" + (f"\n错误码: {err}" if err else "")


def _send_email(to_addr: str, subject: str, body: str) -> None:
    """使用 SMTP 发送邮件；未配置则跳过。"""
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not host or not user or not password:
        raise RuntimeError("SMTP not configured (SMTP_HOST/SMTP_USER/SMTP_PASSWORD)")
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(user, [to_addr], msg.as_string())
