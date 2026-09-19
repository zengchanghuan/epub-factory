"""Opt-in completion mail with a persistent, bounded outbox.

SMTP runs only in the dispatcher, never in the conversion success/failure path.
A lease prevents concurrent workers from delivering the same item. Like other
SMTP outboxes this is at-least-once: a process crash after SMTP acceptance but
before committing `sent` can cause a duplicate. A stable Message-ID helps clients.
"""

import hashlib
import json
import logging
import os
import re
import smtplib
import ssl
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from app.storage import job_store
from .email_subscription_repository import EmailSubscriptionRepository

logger = logging.getLogger("epub_factory")
TERMINAL_STATUSES = {"success", "failed", "cancelled"}
MAX_ADDRESSES = 3
MAX_DELIVERIES = 3
MAX_ATTEMPTS = 5
LEASE_SECONDS = 600


class EmailUnavailableError(RuntimeError):
    pass


class EmailRateLimitError(ValueError):
    pass


def _normalize_email(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) > 254 or not value.isascii() or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value
    ):
        raise ValueError("请输入有效的邮箱地址")
    local, domain = value.rsplit("@", 1)
    if (len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in value
            or any(len(label) > 63 or label.startswith("-") or label.endswith("-") for label in domain.split("."))):
        raise ValueError("请输入有效的邮箱地址")
    return local + "@" + domain.lower()


def _site_base_url():
    value = (os.environ.get("SITE_BASE_URL") or os.environ.get("PUBLIC_BASE_URL") or "https://fixepub.com").rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("invalid_site_base_url")
    return value


def _smtp_config(*, require_enabled=True):
    if require_enabled and os.environ.get("NOTIFY_EMAIL_ENABLED", "").lower() not in {"1", "true", "yes"}:
        raise EmailUnavailableError("邮件通知暂未启用，请稍后回到本页下载")
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or user
    if not host or not user or not password or not sender:
        raise EmailUnavailableError("邮件服务尚未配置，请稍后回到本页下载")
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        security = os.environ.get("SMTP_SECURITY", "ssl" if port == 465 else "starttls").lower()
        timeout = float(os.environ.get("SMTP_TIMEOUT_SECONDS", "15"))
        sender = _normalize_email(sender)
        _site_base_url()
        if security not in {"ssl", "starttls"} or not 1 <= port <= 65535 or not 1 <= timeout <= 60 or any(c in host for c in "\r\n"):
            raise ValueError("invalid_smtp_config")
    except (ValueError, TypeError):
        raise EmailUnavailableError("邮件服务配置不完整，请稍后回到本页下载") from None
    return dict(host=host, port=port, security=security, timeout=timeout, user=user, password=password, sender=sender)


def email_capabilities():
    try:
        _smtp_config()
    except EmailUnavailableError as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "reason": ""}


def _public(record):
    record = record or {}
    return {
        "email": record.get("email", ""),
        "enabled": bool(record.get("email")),
        "subscribed": bool(record.get("email")),
        "status": record.get("status", "unsubscribed"),
        "sent_at": record.get("sent_at"),
        "last_error_code": record.get("last_error_code"),
        **email_capabilities(),
    }


def get_email_subscription(job_id):
    return _public(EmailSubscriptionRepository(job_store).get(job_id))


def set_email_subscription(job_id, email):
    return set_email_subscriptions([job_id], email)[0]


def set_email_subscriptions(job_ids, email):
    """Authorized router must validate ownership of every job before calling."""
    job_ids = list(dict.fromkeys(job_ids))
    max_files = max(2, int(os.environ.get("BATCH_MAX_FILES", "10")))
    if not job_ids or len(job_ids) > max_files:
        raise ValueError(f"一次最多设置 {max_files} 本书的邮件通知")
    email = _normalize_email(email)
    if email:
        _smtp_config()

    def change(job_id, old):
        old = old or {"job_id": job_id, "address_changes": 0, "deliveries": 0}
        if old.get("email", "") == email:
            return old
        if email and (old.get("address_changes", 0) >= MAX_ADDRESSES or old.get("deliveries", 0) >= MAX_DELIVERIES):
            raise EmailRateLimitError("该订单邮件设置次数已达上限，请直接回到本页下载")
        old.update(email=email, status="subscribed" if email else "unsubscribed", event_key="", payload={},
                   attempts=0, next_attempt_at=0, lease_id="", lease_until=0, sent_at=None, last_error_code=None)
        if email:
            old["address_changes"] = old.get("address_changes", 0) + 1
        return old

    rows = EmailSubscriptionRepository(job_store).mutate_many(job_ids, change)
    # State is saved before reconciling. No SMTP operation occurs in this request.
    for row in rows:
        if row and row.get("email"):
            try:
                _reconcile_one(row["job_id"])
            except Exception:
                # The durable opt-in is already saved atomically; the worker will
                # reconcile it later. Do not turn a saved batch into a partial HTTP failure.
                logger.warning("email subscription reconciliation deferred", extra={"job_id": row["job_id"]})
    return [get_email_subscription(job_id) for job_id in job_ids]


def _completion_snapshot(job_id):
    if job_id.startswith("repair:"):
        real_id = job_id[7:]
        if not re.fullmatch(r"[0-9a-f]{32}", real_id):
            return None
        path = Path(os.environ.get("REPAIR_UPLOAD_DIR", "/tmp/epub-repair")) / real_id / "order.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        status = "success" if data.get("status") == "repaired" else data.get("status", "pending")
        return {"status": status, "result_url": _site_base_url() + "/epub-repair.html?" + urlencode({"job_id": real_id}),
                "completion_key": "repair:" + status}
    job = job_store.get(job_id)
    if not job:
        return None
    status = getattr(job.status, "value", str(job.status))
    token = getattr(job, "access_token", "")
    expires = getattr(job, "token_expires_at", None)
    if expires is not None:
        expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
        if expires <= datetime.now(timezone.utc):
            token = ""  # Preserve expiry; never mail an already unusable download capability.
    result_url = (_site_base_url() + "/?" + urlencode({"job_id": job_id}) + "#" + urlencode({"access_token": token})) if token else ""
    attempt = str((getattr(job, "translation_stats", {}) or {}).get("attempt_id") or "")
    updated = job.updated_at.replace(tzinfo=timezone.utc) if job.updated_at.tzinfo is None else job.updated_at
    return {"status": status, "result_url": result_url,
            "completion_key": status + ":" + (attempt or updated.isoformat())}


def queue_completion_email(job_id, status, source_filename=None, result_url=None, completion_key=None):
    """Queue an opted-in terminal result; contains no filename, path or raw error."""
    status = getattr(status, "value", str(status))
    if status == "repaired":
        status = "success"
    if status not in TERMINAL_STATUSES:
        return False
    snapshot = _completion_snapshot(job_id)
    if snapshot:
        if snapshot["status"] not in TERMINAL_STATUSES or snapshot["status"] != status:
            return False  # A stale worker must not enqueue an old attempt.
        result_url = snapshot["result_url"]
        completion_key = snapshot["completion_key"]
    if not result_url:
        def unavailable(old):
            if old and old.get("email") and old.get("last_error_code") != "result_link_unavailable":
                old.update(status="failed", last_error_code="result_link_unavailable", lease_id="", lease_until=0)
                return old
        EmailSubscriptionRepository(job_store).mutate(job_id, unavailable)
        return False
    # Only the configured service origin is allowed, including explicit repair hooks.
    if not result_url.startswith(_site_base_url() + "/"):
        raise ValueError("invalid_result_url")
    event_key = hashlib.sha256((str(completion_key or status) + ":" + status).encode()).hexdigest()

    def queue(old):
        if not old or not old.get("email") or old.get("event_key") == event_key:
            return None
        if old.get("deliveries", 0) >= MAX_DELIVERIES:
            return None
        old.update(event_key=event_key, payload={"job_id": job_id, "status": status, "result_url": result_url},
                   status="pending", attempts=0, next_attempt_at=0, lease_id="", lease_until=0,
                   sent_at=None, last_error_code=None)
        return old

    return EmailSubscriptionRepository(job_store).mutate(job_id, queue) is not None


def _reconcile_one(job_id):
    snapshot = _completion_snapshot(job_id)
    if snapshot and snapshot["status"] in TERMINAL_STATUSES:
        return queue_completion_email(job_id, **snapshot)
    # A manual retry supersedes a queued failure. Wait for its new terminal state.
    if snapshot:
        def clear(old):
            if old and old.get("email") and old.get("status") in {"pending", "retry", "sending", "failed"}:
                old.update(status="subscribed", event_key="", payload={}, lease_id="", lease_until=0)
                return old
        EmailSubscriptionRepository(job_store).mutate(job_id, clear)
    return False


def _email_body(payload):
    status = payload["status"]
    if status == "success":
        summary = "您的任务已完成。请打开下方订单页面查看结果并下载。"
    elif status == "cancelled":
        summary = "您的任务已取消。请打开下方订单页面查看状态。"
    else:
        summary = "您的任务暂未完成。请打开下方订单页面查看状态，或联系 QQ 249998620 获取帮助。"
    return summary + "\n\n" + payload["result_url"] + "\n\n此链接仅供您本人使用，请勿转发。文件与链接保留期限以网站提示为准。\n此邮件由您为该订单主动设置的通知触发。"


def _send_email(to_addr, subject, body, message_id, *, require_enabled=True):
    config = _smtp_config(require_enabled=require_enabled)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config["sender"]
    msg["To"] = to_addr
    msg["Message-ID"] = message_id
    msg.set_content(body)
    context = ssl.create_default_context()
    if config["security"] == "ssl":
        client = smtplib.SMTP_SSL(config["host"], config["port"], timeout=config["timeout"], context=context)
    else:
        client = smtplib.SMTP(config["host"], config["port"], timeout=config["timeout"])
    with client as smtp:
        if config["security"] == "starttls":
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(config["user"], config["password"])
        refused = smtp.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def _error_code(exc):
    if isinstance(exc, EmailUnavailableError):
        return "email_unavailable"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "smtp_authentication_failed"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "smtp_recipient_refused"
    if isinstance(exc, (TimeoutError, OSError)):
        return "smtp_connection_failed"
    return "smtp_delivery_failed"


def dispatch_pending_email_notifications(limit=20):
    """Periodic worker entry: reconcile lost hooks, lease items, retry boundedly."""
    counts = {"sent": 0, "failed": 0, "retried": 0, "unavailable": 0}
    if not email_capabilities()["available"]:
        counts["unavailable"] = 1
        return counts
    repository = EmailSubscriptionRepository(job_store)
    rows = repository.list()
    for row in rows:
        if row.get("email"):
            try:
                _reconcile_one(row["job_id"])
            except Exception:
                logger.warning("email reconciliation failed", extra={"job_id": row["job_id"]})
    processed = 0
    for candidate in repository.list():
        if processed >= max(1, min(int(limit), 100)):
            break
        now = time.time()
        lease = uuid.uuid4().hex

        def claim(old):
            if not old or not old.get("email") or not old.get("payload") or old.get("deliveries", 0) >= MAX_DELIVERIES:
                return None
            if old.get("status") not in {"pending", "retry", "sending"}:
                return None
            if old.get("next_attempt_at", 0) > now or old.get("lease_until", 0) > now:
                return None
            if old.get("attempts", 0) >= MAX_ATTEMPTS:
                old.update(status="failed", last_error_code="delivery_attempts_exhausted", lease_id="", lease_until=0)
                return old
            old.update(status="sending", lease_id=lease, lease_until=now + LEASE_SECONDS, attempts=old.get("attempts", 0) + 1)
            return old

        claimed = repository.mutate(candidate["job_id"], claim)
        if not claimed or claimed.get("lease_id") != lease:
            continue
        processed += 1
        # Recheck cancellation or address edits after claiming; SMTP itself cannot
        # participate in the database transaction.
        current = repository.get(claimed["job_id"])
        if not current or current.get("lease_id") != lease:
            continue
        error = None
        try:
            subject = "[FixEpub] 任务已完成" if claimed["payload"]["status"] == "success" else "[FixEpub] 任务状态通知"
            identity = hashlib.sha256((claimed["job_id"] + claimed["event_key"] + claimed["email"]).encode()).hexdigest()
            _send_email(claimed["email"], subject, _email_body(claimed["payload"]), "<" + identity + "@fixepub.com>")
        except Exception as exc:
            error = _error_code(exc)
            logger.warning("completion email delivery failed", extra={"job_id": claimed["job_id"], "error_code": error})

        def finish(old):
            if not old or old.get("lease_id") != lease:
                return None
            old.update(lease_id="", lease_until=0, last_error_code=error)
            if not error:
                old.update(status="sent", sent_at=datetime.now(timezone.utc).isoformat(), deliveries=old.get("deliveries", 0) + 1)
            elif old["attempts"] >= MAX_ATTEMPTS:
                old.update(status="failed")
            else:
                old.update(status="retry", next_attempt_at=time.time() + min(3600, 60 * 2 ** (old["attempts"] - 1)))
            return old

        finished = repository.mutate(claimed["job_id"], finish)
        if finished:
            counts[{"sent": "sent", "failed": "failed", "retry": "retried"}[finished["status"]]] += 1
    return counts
