"""Verified-payment merchant mail, kept separate from customer completion mail.

Only trusted payment verification code may enqueue. One durable event is created
per merchant order number; batching does not generate per-book messages. SMTP is
at-least-once: a crash after acceptance but before recording sent may duplicate a
message. A stable Message-ID helps clients; it is not exactly-once delivery.
"""
import hashlib
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.storage import job_store
from . import completion_email_service as completion_mail
from .payment_email_repository import PaymentEmailRepository

logger = logging.getLogger("epub_factory")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 600
BEIJING = timezone(timedelta(hours=8))
ORDER_KINDS = {"translation": "AI 翻译", "conversion": "EPUB 转换", "repair": "EPUB 修复",
               "batch": "批量处理", "batch_translation": "批量 AI 翻译", "batch_conversion": "批量 EPUB 转换",
               "polish": "AI 精校"}


def _enabled():
    return os.environ.get("OWNER_PAYMENT_EMAIL_ENABLED", "1").lower() in {"1", "true", "yes"}


def _recipient():
    value = completion_mail._normalize_email(os.environ.get("OWNER_PAYMENT_EMAIL_TO", "249998620@qq.com"))
    if not value:
        raise ValueError("owner_email_missing")
    return value


def payment_email_capabilities():
    if not _enabled():
        return {"available": False, "reason": "owner_email_disabled"}
    try:
        _recipient()
        completion_mail._smtp_config(require_enabled=False)
    except (ValueError, completion_mail.EmailUnavailableError):
        return {"available": False, "reason": "owner_email_unavailable"}
    return {"available": True, "reason": ""}


def _normalize_paid_at(value):
    if value is None:
        value = datetime.now(timezone.utc)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("invalid_payment_time")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"


def queue_paid_order_email(order_no, amount, order_kind, file_count=1, paid_at=None, is_test_order=False):
    """Called only after server-side signature/account/amount verification.

No SMTP and no historical-order scan here. Errors are contained so notification
failures never change a verified payment result. Disabled/test orders are ignored.
SMTP configuration may follow later: newly verified receipts remain durable.
"""
    if is_test_order or not _enabled():
        return False
    try:
        if not isinstance(order_no, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", order_no):
            raise ValueError("invalid_order_no")
        value = Decimal(str(amount))
        if not value.is_finite() or value <= 0 or value != value.quantize(Decimal("0.01")):
            raise ValueError("invalid_order_amount")
        if isinstance(file_count, bool) or not isinstance(file_count, int) or not 1 <= file_count <= 1000:
            raise ValueError("invalid_file_count")
        recipient = _recipient()
        now = time.time()
        record = dict(order_no=order_no, email=recipient, status="pending", attempts=0,
                      next_attempt_at=0, lease_id="", lease_until=0, created_at=now,
                      sent_at=None, last_error_code=None,
                      payload=dict(amount=format(value, ".2f"), order_kind=ORDER_KINDS.get(str(order_kind), "订单"),
                                   file_count=file_count, paid_at=_normalize_paid_at(paid_at)))
        queued = PaymentEmailRepository(job_store).mutate(order_no, lambda old: record if old is None else None)
    except Exception:
        # Do not log recipient, credentials, callback parameters or provider text.
        logger.warning("Payment email queue unavailable; payment processing continues")
        return False
    if queued:
        try:
            from .payment_email_worker import payment_email_worker
            payment_email_worker.wake()
        except Exception:
            logger.warning("Payment email wake deferred; durable receipt retained")
    return queued is not None


def _body(record):
    payload = record["payload"]
    return ("有一笔订单已通过服务端支付验证。\n\n"
            f"实付金额：¥{payload['amount']}\n"
            f"商户订单号：{record['order_no']}\n"
            f"订单类型：{payload['order_kind']}\n"
            f"文件数量：{payload['file_count']} 本\n"
            f"确认时间：{payload['paid_at']}\n\n"
            "打开管理员看板查看订单（需要管理员登录）：\n"
            "https://fixepub.com/orders-admin.html\n\n"
            "这是付款成功通知；处理进度和结果以看板为准。")


def dispatch_pending_payment_emails(limit=20):
    """Lease only existing outbox entries; never infer new receipts from jobs."""
    counts = {"sent": 0, "failed": 0, "retried": 0, "unavailable": 0}
    if not payment_email_capabilities()["available"]:
        counts["unavailable"] = 1
        return counts
    repository = PaymentEmailRepository(job_store)
    for candidate in repository.due(time.time(), max(1, min(int(limit), 100))):
        now = time.time()
        lease = uuid.uuid4().hex

        def claim(old):
            if not old or old.get("status") not in {"pending", "retry", "sending"}:
                return None
            if old.get("next_attempt_at", 0) > now or old.get("lease_until", 0) > now:
                return None
            if old.get("attempts", 0) >= MAX_ATTEMPTS:
                old.update(status="failed", last_error_code="delivery_attempts_exhausted", lease_id="", lease_until=0)
                return old
            old.update(status="sending", lease_id=lease, lease_until=now + LEASE_SECONDS, attempts=old.get("attempts", 0) + 1)
            return old

        claimed = repository.mutate(candidate["order_no"], claim)
        if not claimed:
            continue
        if claimed.get("lease_id") != lease:
            if claimed.get("status") == "failed":
                counts["failed"] += 1
            continue
        error = None
        try:
            identity = hashlib.sha256(("payment:" + claimed["order_no"]).encode()).hexdigest()
            completion_mail._send_email(claimed["email"], f"[FixEpub] 新订单已付款 ¥{claimed['payload']['amount']}",
                                        _body(claimed), "<" + identity + "@fixepub.com>", require_enabled=False)
        except Exception as exc:
            error = completion_mail._error_code(exc)
            logger.warning("Payment email delivery failed", extra={"error_code": error})

        def finish(old):
            if not old or old.get("lease_id") != lease:
                return None
            old.update(lease_id="", lease_until=0, last_error_code=error)
            if error is None:
                old.update(status="sent", sent_at=datetime.now(timezone.utc).isoformat())
            elif old["attempts"] >= MAX_ATTEMPTS:
                old.update(status="failed")
            else:
                old.update(status="retry", next_attempt_at=time.time() + min(3600, 60 * 2 ** (old["attempts"] - 1)))
            return old

        finished = repository.mutate(claimed["order_no"], finish)
        if finished:
            counts[{"sent": "sent", "failed": "failed", "retry": "retried"}[finished["status"]]] += 1
    return counts
