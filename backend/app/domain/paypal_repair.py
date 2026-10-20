"""PayPal adapter for durable repair orders; their random IDs are bearer capabilities.

Provider and currency are frozen at diagnosis. Never switch an existing Alipay
order, and never infer payment from browser callbacks or webhook event bodies.
"""
import re
from types import SimpleNamespace
from fastapi import HTTPException
from . import paypal_checkout as checkout
from ..infra import paypal


def bound_job(job_id, data):
    return SimpleNamespace(id="repair_" + job_id, batch_id=None,
        expected_amount=data.get("expected_amount"),
        translation_stats={"payment_method": data.get("payment_method")})


def query_trade(store, job_id, data):
    if data.get("currency") != "USD" or data.get("payment_method") != "paypal":
        raise paypal.PayPalUnavailable("修复订单支付方式不符")
    return checkout.query_trade(store, bound_job(job_id, data))


def load(main, local_id):
    if not re.fullmatch(r"repair_[0-9a-f]{32}", local_id):
        raise HTTPException(404, "订单不存在")
    job_id = local_id[7:]
    data = main._repair_job_get(job_id)
    if not data:
        raise HTTPException(404, "订单不存在或已过期")
    if data.get("payment_method") != "paypal" or data.get("currency") != "USD":
        raise HTTPException(409, "原订单不支持 PayPal，请使用原支付方式")
    if data.get("out_trade_no") != local_id:
        raise HTTPException(409, "请先从修复页面发起付款")
    paypal.money(data.get("expected_amount"))
    return job_id, data


def payable(main, job_id, data):
    if data.get("status") != "pending_payment":
        raise HTTPException(409, "订单当前不可付款，请查看任务状态")
    _, can_pay = main._repair_diagnosis(job_id, data, refresh=True)
    if not can_pay:
        raise HTTPException(409, "源文件已失效或没有可修复问题，请勿付款")


def settle_verified(main, local_id, source="verified_query"):
    job_id, data = load(main, local_id)
    trade = query_trade(main.job_store, job_id, data)
    if trade.get("trade_status") != "TRADE_SUCCESS":
        raise paypal.PayPalUnavailable("付款尚未核验，请稍后重试")
    main._repair_confirm_payment(job_id, trade["total_amount"], source)


def handle(main, local_id, action):
    job_id, data = load(main, local_id)
    amount = data["expected_amount"]
    if action == "info":
        return {"local_id": local_id, "amount": amount, "currency": "USD", "file_count": 1,
                "status": data["status"], "kind": "repair", "product": "repair", "job_id": job_id}
    job = bound_job(job_id, data)
    record = checkout._mapping(main.job_store, local_id=local_id)
    if action == "create":
        payable(main, job_id, data)
        if record:
            checkout._check_mapping(record, job)
            order = paypal.get_order(record["order_id"])
            paypal.validate_order(order, local_id, amount, order_id=record["order_id"], merchant_id=record["merchant_id"])
            if order.get("status") == "COMPLETED":
                settle_verified(main, local_id)
                raise HTTPException(409, "原订单已付款，请返回修复任务")
        else:
            order = paypal.create_order(local_id, amount)
            record = checkout._save_mapping(main.job_store, local_id, amount,
                paypal.validate_order(order, local_id, amount))
        return {"id": record["order_id"]}
    checkout._check_mapping(record, job)
    order = paypal.get_order(record["order_id"])
    paypal.validate_order(order, local_id, amount, order_id=record["order_id"], merchant_id=record["merchant_id"])
    if order.get("status") != "COMPLETED":
        payable(main, job_id, data)
        if order.get("status") != "APPROVED":
            raise HTTPException(409, "请先在 PayPal 确认付款")
        order = paypal.capture_order(record["order_id"], local_id)
    paypal.validate_order(order, local_id, amount, order_id=record["order_id"], merchant_id=record["merchant_id"], completed=True)
    main._repair_confirm_payment(job_id, amount, "verified_query")
    return {"status": "COMPLETED", "local_id": local_id, "currency": "USD", "amount": amount}
