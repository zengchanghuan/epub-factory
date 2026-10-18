"""Fixed USD conversion checkout, bound to existing authenticated job lifecycles."""
from datetime import datetime, timezone
import json
import re

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..infra import paypal
from ..models import JobStatus
from .checkout_resume import CheckoutUnavailable, require_open_checkout, original_checkout
from .payment_entitlement import manual_payment_guard


def is_paypal(job):
    return (job.translation_stats or {}).get("payment_method") == "paypal"


def currency(job):
    return "USD" if is_paypal(job) else "CNY"


def select_method(value, *, translation=False, polish=False, store=None):
    # FastAPI Form defaults are only coerced by HTTP, so keep direct legacy calls valid.
    if not isinstance(value, str):
        value = "alipay"
    if value not in {"alipay", "paypal"}:
        raise HTTPException(400, "支付方式无效")
    if value == "paypal":
        if translation or polish:
            raise HTTPException(400, "PayPal 暂仅支持普通转换与 PDF 保留原文转换")
        if not paypal.enabled() or getattr(store, "_engine", None) is None:
            raise HTTPException(503, "PayPal 暂未开放，请选择支付宝")
    return value


def checkout_url(local_id):
    if not re.fullmatch(r"(?:batch_)?[a-zA-Z0-9_-]{1,64}", local_id):
        raise ValueError("invalid local order")
    return "https://fixepub.com/paypal-checkout.html?order=" + local_id


def _mapping(store, *, local_id=None, order_id=None):
    from ..storage_db import PayPalOrderRecord
    with Session(store._engine) as session:
        record = (session.get(PayPalOrderRecord, local_id) if local_id else
                  session.scalar(select(PayPalOrderRecord).where(PayPalOrderRecord.order_id == order_id)))
        return {key: getattr(record, key) for key in ("local_id", "order_id", "merchant_id", "amount", "mode")} if record else None


def _save_mapping(store, local_id, amount, receipt):
    from ..storage_db import PayPalOrderRecord
    values = dict(local_id=local_id, amount=amount, mode=paypal.settings()[0],
                  order_id=receipt["order_id"], merchant_id=receipt["merchant_id"])
    try:
        with Session(store._engine) as session:
            session.add(PayPalOrderRecord(**values, created_at=datetime.now(timezone.utc)))
            session.commit()
    except IntegrityError:
        if _mapping(store, local_id=local_id) != values:
            raise paypal.PayPalUnavailable("支付订单绑定冲突，请联系客服") from None
    return values


def _check_mapping(record, job):
    if (not record or record["mode"] != paypal.settings()[0]
            or record["amount"] != job.expected_amount or not is_paypal(job)):
        raise paypal.PayPalUnavailable("PayPal 订单配置与原报价不符，请联系客服")


def query_trade(store, job):
    """Read-only verification used by recovery, admin and scheduled reconciliation."""
    local_id = "batch_" + job.batch_id if job.batch_id else job.id
    record = _mapping(store, local_id=local_id)
    if not record:
        return {"out_trade_no": local_id, "trade_status": "NOT_CREATED"}
    _check_mapping(record, job)
    order = paypal.get_order(record["order_id"])
    done = order.get("status") == "COMPLETED"
    receipt = paypal.validate_order(order, local_id, record["amount"], order_id=record["order_id"],
                                    merchant_id=record["merchant_id"], completed=done)
    return {"out_trade_no": local_id, "trade_status": "TRADE_SUCCESS" if done else "WAIT_BUYER_PAY",
            "total_amount": record["amount"], "currency": "USD", "trade_no": receipt["capture_id"]}


def safe_query_trade(store, job):
    try:
        return query_trade(store, job)
    except paypal.PayPalUnavailable:
        return None


def _rows(main, local_id, request=None):
    if not re.fullmatch(r"(?:batch_)?[a-zA-Z0-9_-]{1,64}", local_id):
        raise HTTPException(404, "订单不存在")
    rows = (main._list_batch_jobs(local_id[6:]) if local_id.startswith("batch_")
            else [main.job_store.get(local_id)])
    if not rows or any(row is None for row in rows):
        raise HTTPException(404, "订单不存在")
    leader = rows[0]
    if leader.batch_id:
        if (local_id != "batch_" + leader.batch_id or len(rows) != leader.batch_size
                or [row.batch_index for row in rows] != list(range(len(rows)))):
            raise HTTPException(409, "批次信息不完整，不能单独付款")
    for row in rows:
        if request is not None:
            main._require_checkout_access(request, row)
        if not is_paypal(row) or row.enable_translation or row.enable_precision_polish:
            raise HTTPException(409, "订单不支持 PayPal 支付")
    paypal.money(leader.expected_amount)
    if leader.status == JobStatus.pending_payment:
        original_checkout(leader, local_id, leader.expected_amount)
    return rows


def _settle(main, rows, *, source="verified_query"):
    job = rows[0]
    local_id = "batch_" + job.batch_id if job.batch_id else job.id
    main._record_verified_payment(local_id, job.expected_amount, "batch" if job.batch_id else "conversion",
        source=source, file_count=len(rows), is_test_order=any(row.is_test_order for row in rows), currency="USD")
    if job.batch_id:
        main._release_batch(job.batch_id, source=source, amount=job.expected_amount)
    else:
        main.job_store.settle_verified_payment(job.id, amount=job.expected_amount, source=source)
        main._enqueue_conversion(main.job_store.get(job.id) or job)


def resume(main, job, request, *, batch_id=None):
    local_id = "batch_" + batch_id if batch_id else job.id
    rows = _rows(main, local_id, request)
    trade = query_trade(main.job_store, rows[0])
    if trade.get("trade_status") == "TRADE_SUCCESS":
        _settle(main, rows)
        rows = _rows(main, local_id, request)
    payable = all(row.status == JobStatus.pending_payment and not manual_payment_guard(row) for row in rows)
    if payable:
        require_open_checkout(rows, artifact_root=main.OUTPUT_DIR)
    payload = main._batch_payload(batch_id, rows) if batch_id else main._job_to_v2_detail(rows[0], None)
    return {**payload, "amount": rows[0].expected_amount, "currency": "USD", "payment_method": "paypal",
            "checkout_available": payable, "pay_url": checkout_url(local_id) if payable else None, "qr_code": None}


def router(main):
    routes = APIRouter()

    def guard(request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != "https://fixepub.com":
            raise HTTPException(403, "请求来源无效")

    def fail(exc):
        return HTTPException(getattr(exc, "status_code", 503), str(exc))

    @routes.get("/api/v2/paypal/config")
    def config():
        available = paypal.enabled() and getattr(main.job_store, "_engine", None) is not None
        return {"enabled": available, "client_id": paypal.settings()[1] if available else None,
                "currency": "USD", "unit_amount": "0.99", "products": ["conversion", "pdf_conversion"]}

    @routes.get("/api/v2/paypal/orders/{local_id}")
    def info(local_id: str, request: Request):
        try:
            rows = _rows(main, local_id, request)
        except (paypal.PayPalUnavailable, CheckoutUnavailable) as exc:
            raise fail(exc) from None
        job = rows[0]
        return {"local_id": local_id, "amount": job.expected_amount, "currency": "USD", "file_count": len(rows),
                "status": main._job_to_v2_status(job), "kind": "batch" if job.batch_id else "job",
                "job_id": job.id, "batch_id": job.batch_id or None}

    @routes.post("/api/v2/paypal/orders/{local_id}")
    def create(local_id: str, request: Request):
        guard(request)
        try:
            rows = _rows(main, local_id, request)
            job = rows[0]
            if any(row.status != JobStatus.pending_payment or manual_payment_guard(row) for row in rows):
                raise HTTPException(409, "订单已完成或不可付款，请刷新状态")
            require_open_checkout(rows, artifact_root=main.OUTPUT_DIR)
            record = _mapping(main.job_store, local_id=local_id)
            if record:
                _check_mapping(record, job)
                order = paypal.get_order(record["order_id"])
                paypal.validate_order(order, local_id, job.expected_amount, order_id=record["order_id"], merchant_id=record["merchant_id"])
                if order.get("status") == "COMPLETED":
                    if query_trade(main.job_store, job).get("trade_status") == "TRADE_SUCCESS":
                        _settle(main, rows)
                    raise HTTPException(409, "此订单已付款，请返回查看任务")
            else:
                order = paypal.create_order(local_id, job.expected_amount, file_count=len(rows))
                record = _save_mapping(main.job_store, local_id, job.expected_amount,
                    paypal.validate_order(order, local_id, job.expected_amount))
            return {"id": record["order_id"]}
        except (paypal.PayPalUnavailable, CheckoutUnavailable) as exc:
            raise fail(exc) from None

    @routes.post("/api/v2/paypal/orders/{local_id}/capture")
    def capture(local_id: str, request: Request):
        guard(request)
        try:
            rows = _rows(main, local_id, request)
            job = rows[0]
            record = _mapping(main.job_store, local_id=local_id)
            _check_mapping(record, job)
            order = paypal.get_order(record["order_id"])
            paypal.validate_order(order, local_id, job.expected_amount, order_id=record["order_id"], merchant_id=record["merchant_id"])
            if order.get("status") != "COMPLETED":
                if order.get("status") != "APPROVED" or any(row.status != JobStatus.pending_payment or manual_payment_guard(row) for row in rows):
                    raise HTTPException(409, "请先在 PayPal 确认订单，或刷新查看任务状态")
                require_open_checkout(rows, artifact_root=main.OUTPUT_DIR)
                order = paypal.capture_order(record["order_id"], local_id)
            paypal.validate_order(order, local_id, job.expected_amount, order_id=record["order_id"],
                                   merchant_id=record["merchant_id"], completed=True)
            _settle(main, rows)
            return {"status": "COMPLETED", "local_id": local_id, "currency": "USD", "amount": job.expected_amount}
        except (paypal.PayPalUnavailable, CheckoutUnavailable) as exc:
            raise fail(exc) from None

    @routes.post("/api/v2/webhooks/paypal", include_in_schema=False)
    async def webhook(request: Request):
        # Bound before decoding; the event itself is never payment evidence.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 128 * 1024:
                raise HTTPException(413, "Payload too large")
        try:
            event = json.loads(body)
            if not isinstance(event, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Invalid event") from None
        try:
            import asyncio
            verified = await asyncio.to_thread(paypal.verify_webhook, request.headers, event)
            if not verified:
                raise HTTPException(400, "Invalid signature")
            if event.get("event_type") != "PAYMENT.CAPTURE.COMPLETED":
                return {"received": True}
            order_id = ((event.get("resource") or {}).get("supplementary_data") or {}).get("related_ids", {}).get("order_id")
            paypal.identifier(order_id)
            record = _mapping(main.job_store, order_id=order_id)
            if not record:
                return {"received": True}
            rows = _rows(main, record["local_id"])
            trade = await asyncio.to_thread(query_trade, main.job_store, rows[0])
            if trade.get("trade_status") != "TRADE_SUCCESS":
                raise HTTPException(503, "Payment not confirmed")
            await asyncio.to_thread(_settle, main, rows, source="verified_webhook")
            return {"received": True}
        except (paypal.PayPalUnavailable, CheckoutUnavailable) as exc:
            raise fail(exc) from None

    return routes
