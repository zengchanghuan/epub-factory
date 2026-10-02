"""Pure, conservative payment lifecycle transitions; no gateway calls here."""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import ErrorCode, JobStatus
from .payment_entitlement import VERIFIED_SOURCES, manual_payment_guard


LEGACY_TIMEOUT_MESSAGES = frozenset({"支付超时，订单已关闭", "支付超时，批次订单已关闭"})
PAYMENT_REVIEW_MESSAGE = "已核验付款，但订单已取消；待人工核实履约或退款（尚未退款）"


def is_payment_expired(job) -> bool:
    """Do not turn arbitrary user cancellations into automatic paid execution."""
    if job.status != JobStatus.cancelled:
        return False
    resolution = job.payment_resolution or {}
    if manual_payment_guard(job):
        return False
    return (job.error_code == ErrorCode.PAYMENT_EXPIRED.value
            or resolution.get("state") == "closed"
            or job.message in LEGACY_TIMEOUT_MESSAGES)


def settlement_values(job, *, source: str, amount: str, now=None) -> tuple[str, dict]:
    """Caller has verified merchant identity, successful payment and amount."""
    if source not in VERIFIED_SOURCES:
        raise ValueError("Untrusted payment settlement source")
    if manual_payment_guard(job) == "refund_recorded":
        return "unchanged", {}
    at = now or datetime.now(timezone.utc)
    if job.status == JobStatus.pending_payment or is_payment_expired(job):
        action, state = "released", "paid"
    elif job.status == JobStatus.cancelled and (job.payment_resolution or {}).get("state") != "paid_review":
        action, state = "review", "paid_review"
    else:
        return "unchanged", {}
    history = dict(job.payment_resolution or {})
    if history.get("state") == "closed" and history.get("source"):
        history.setdefault("closed_source", history["source"])
    if action == "review":
        history.setdefault("original_cancel_message", job.message)
        history.setdefault("original_error_code", job.error_code)
    resolution = {**history, "state": state, "source": source,
                  "amount": str(amount), "verified_at": at.isoformat(), "updated_at": at.isoformat()}
    return action, {
        "status": JobStatus.pending if action == "released" else JobStatus.cancelled,
        "message": "支付成功，排队中..." if action == "released" else PAYMENT_REVIEW_MESSAGE,
        "error_code": None if action == "released" else ErrorCode.PAYMENT_REVIEW_REQUIRED.value,
        "payment_resolution": resolution,
        "updated_at": at,
    }


def closed_values(job, *, batch=False, now=None) -> dict:
    at = now or datetime.now(timezone.utc)
    return {
        "status": JobStatus.cancelled,
        "message": "支付超时，批次订单已关闭" if batch else "支付超时，订单已关闭",
        "error_code": ErrorCode.PAYMENT_EXPIRED.value,
        "payment_resolution": {**dict(job.payment_resolution or {}), "state": "closed",
                               "source": "verified_gateway_close", "closed_source": "verified_gateway_close",
                               "closed_at": at.isoformat(),
                               "updated_at": at.isoformat()},
        "updated_at": at,
    }
