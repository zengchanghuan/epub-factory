"""Read-only checkout guards; payment facts and dispatch use existing services."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


class CheckoutUnavailable(ValueError):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


def valid_checkout_url(value):
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def checkout_snapshot(order_no, amount, *, pay_url=None, qr_code=None):
    """Remember the successful payment product, not a newly guessed one."""
    if bool(pay_url) == bool(qr_code) or not valid_checkout_url(qr_code or pay_url):
        raise ValueError("invalid checkout response")
    return {"schema_version": 1, "order_no": order_no, "amount": amount,
            "channel": "qr" if qr_code else "page", **({"qr_code": qr_code} if qr_code else {})}


def original_checkout(job, order_no, amount):
    stats = job.translation_stats or {}
    if "payment_checkout" not in stats:
        # Historical translation creation always used page-pay. Conversion and
        # batch creation could have used either product; NOT_CREATED does not
        # establish which QR/link the user originally received.
        if job.enable_translation and not job.batch_id:
            return {"schema_version": 1, "order_no": order_no, "amount": amount, "channel": "page"}
        raise CheckoutUnavailable("旧订单未记录原支付通道，请联系客服核验；不会切换通道或另建付款订单")
    saved = stats["payment_checkout"]
    if (not isinstance(saved, dict) or type(saved.get("schema_version")) is not int
            or saved["schema_version"] != 1 or saved.get("order_no") != order_no
            or saved.get("amount") != amount or saved.get("channel") not in {"page", "qr"}
            or (saved["channel"] == "qr" and not valid_checkout_url(saved.get("qr_code")))
            or (saved["channel"] == "page" and saved.get("qr_code"))):
        raise CheckoutUnavailable("原支付通道记录不完整或与订单不符，请联系客服核验；请勿再次付款")
    return dict(saved)


def frozen_amount(job):
    value = str(job.expected_amount or "").strip()
    try:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            raise ValueError()
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise CheckoutUnavailable("原订单金额缺失或无效，请联系客服核验；不会重新计价") from None
    return value


def classify_trade(trade, order_no, amount):
    if not isinstance(trade, dict) or trade.get("out_trade_no") != order_no:
        raise CheckoutUnavailable("暂时无法核验支付状态，请稍后重试；请勿重复付款", 503)
    status = trade.get("trade_status")
    if status in {"NOT_CREATED", "TRADE_CLOSED"}:
        return status
    if status not in {"WAIT_BUYER_PAY", "TRADE_SUCCESS", "TRADE_FINISHED"}:
        raise CheckoutUnavailable("暂时无法核验支付状态，请稍后重试；请勿重复付款", 503)
    try:
        paid = Decimal(str(trade.get("total_amount") or ""))
        if not paid.is_finite() or paid <= 0 or paid != Decimal(amount):
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise CheckoutUnavailable("支付宝订单金额与原报价不符，请联系客服核验；请勿再次付款") from None
    return status


def require_open_checkout(jobs, *, now=None):
    """Only unpaid usable orders may produce another link; never revive expiry."""
    now = now or datetime.now(timezone.utc)
    try:
        hours = int(os.environ.get("RECONCILE_TIMEOUT_HOURS", "2"))
        if not 1 <= hours <= 24 * 365:
            raise ValueError()
    except ValueError:
        raise CheckoutUnavailable("支付有效期配置未就绪，请联系管理员", 503) from None
    for job in jobs:
        created = job.created_at
        if not isinstance(created, datetime):
            raise CheckoutUnavailable("原订单时间缺失，请联系客服核验")
        created = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created.astimezone(timezone.utc)
        checkout = (job.translation_stats or {}).get("payment_checkout")
        # The existing precreate integration uses the provider's two-hour QR
        # lifetime. A longer reconciliation timeout must not revive that code.
        checkout_hours = min(hours, 2) if isinstance(checkout, dict) and checkout.get("channel") == "qr" else hours
        if now >= created + timedelta(hours=checkout_hours):
            # Local expiry is not gateway closure and is not written as such.
            raise CheckoutUnavailable("原订单已超过支付有效期，请勿再次付款；已付款可点击恢复状态，未付款请重新上传")
        source = Path(job.input_path or "")
        try:
            available = source.is_file() and source.stat().st_size > 0
        except OSError:
            available = False
        if not available:
            raise CheckoutUnavailable("原订单文件已不可用，请勿付款，请重新上传文件")
