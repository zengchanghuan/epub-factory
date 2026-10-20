"""PayPal Orders v2. Secrets and provider response bodies never enter logs."""
import hashlib
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests


class PayPalUnavailable(ValueError):
    pass


_lock = threading.Lock()
_token = None
_token_key = None
_expires = 0


def settings():
    mode = os.environ.get("PAYPAL_MODE", "sandbox").strip()
    if mode not in {"sandbox", "live"}:
        raise PayPalUnavailable("PayPal 环境配置无效")
    client = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    secret = (os.environ.get("PAYPAL_CLIENT_SECRET") or os.environ.get("PAYPAL_SECRET") or "").strip()
    if not client or not secret:
        raise PayPalUnavailable("PayPal 尚未配置")
    return mode, client, secret


def enabled():
    try:
        settings()
        return (os.environ.get("PAYPAL_CHECKOUT_ENABLED", "0").lower() in {"1", "true", "yes"}
                and bool(os.environ.get("PAYPAL_WEBHOOK_ID", "").strip()))
    except PayPalUnavailable:
        return False


def money(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,8}\.[0-9]{2}", value):
            raise ValueError()
        result = Decimal(value)
        if result <= 0:
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise PayPalUnavailable("PayPal 金额无效") from None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{8,40}", value):
        raise PayPalUnavailable("PayPal 订单标识无效")
    return value


def _base(mode):
    return "https://api-m.paypal.com" if mode == "live" else "https://api-m.sandbox.paypal.com"


def _access_token():
    global _token, _token_key, _expires
    mode, client, secret = settings()
    key = (mode, client, hashlib.sha256(secret.encode()).digest())
    with _lock:
        if _token_key == key and _token and time.monotonic() < _expires:
            return _token
        try:
            response = requests.post(_base(mode) + "/v1/oauth2/token", auth=(client, secret),
                data={"grant_type": "client_credentials"}, timeout=(5, 20), allow_redirects=False)
            if response.status_code != 200:
                raise ValueError()
            data = response.json()
            token = data["access_token"]
            lifetime = int(data["expires_in"])
            if not isinstance(token, str) or not token or lifetime <= 60:
                raise ValueError()
        except Exception:
            raise PayPalUnavailable("PayPal 授权暂不可用，请稍后重试") from None
        _token, _token_key, _expires = token, key, time.monotonic() + min(lifetime - 60, 3600)
        return token


def api(method, path, *, body=None, request_id=None):
    mode, _, _ = settings()
    headers = {"Authorization": "Bearer " + _access_token(), "Content-Type": "application/json",
               "Prefer": "return=representation"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    try:
        response = requests.request(method, _base(mode) + path, headers=headers, json=body,
                                    timeout=(5, 25), allow_redirects=False)
        if response.status_code not in {200, 201}:
            raise ValueError()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except Exception:
        raise PayPalUnavailable("PayPal 暂时无法确认订单，请稍后重试；请勿重复付款") from None


def validate_order(order, local_id, amount, *, order_id=None, merchant_id=None, completed=False):
    """Verify all bindings, not merely a browser's onApprove or a COMPLETED flag."""
    try:
        oid = identifier(order["id"])
        if order_id is not None and oid != order_id:
            raise ValueError()
        units = order["purchase_units"]
        if not isinstance(units, list) or len(units) != 1:
            raise ValueError()
        unit = units[0]
        if unit.get("custom_id") != local_id or unit.get("invoice_id") != "fixepub-" + local_id:
            raise ValueError()
        if unit["amount"]["currency_code"] != "USD" or money(unit["amount"]["value"]) != money(amount):
            raise ValueError()
        merchant = identifier(unit["payee"]["merchant_id"])
        if merchant_id is not None and merchant != merchant_id:
            raise ValueError()
        if completed:
            if order.get("status") != "COMPLETED":
                raise ValueError()
            payments = unit["payments"]
            captures = payments["captures"]
            if payments.get("refunds") or len(captures) != 1:
                raise ValueError()
            capture = captures[0]
            if (capture["status"] != "COMPLETED" or capture.get("final_capture") is not True
                    or capture["amount"]["currency_code"] != "USD"
                    or money(capture["amount"]["value"]) != money(amount)):
                raise ValueError()
            capture_id = identifier(capture["id"])
        else:
            capture_id = None
        return {"order_id": oid, "merchant_id": merchant, "capture_id": capture_id}
    except (KeyError, TypeError, ValueError, IndexError):
        raise PayPalUnavailable("PayPal 付款信息与订单不符，请联系客服核验") from None


def create_order(local_id, amount, *, file_count=1):
    money(amount)
    result = api("POST", "/v2/checkout/orders", request_id="create-" + local_id, body={
        "intent": "CAPTURE", "purchase_units": [{
            "reference_id": local_id, "custom_id": local_id, "invoice_id": "fixepub-" + local_id,
            "description": f"FixEpub ebook processing service ({file_count} file(s))",
            "amount": {"currency_code": "USD", "value": amount},
        }], "payment_source": {"paypal": {"experience_context": {
            "brand_name": "FixEpub", "shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW",
            "return_url": "https://fixepub.com/paypal-checkout.html?order=" + local_id,
            "cancel_url": "https://fixepub.com/",
        }}},
    })
    validate_order(result, local_id, amount)
    return result


def get_order(order_id):
    return api("GET", "/v2/checkout/orders/" + identifier(order_id))


def capture_order(order_id, local_id):
    return api("POST", "/v2/checkout/orders/" + identifier(order_id) + "/capture",
               body={}, request_id="capture-" + local_id)


def verify_webhook(headers, event):
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID", "").strip()
    if not webhook_id:
        return False
    names = {"auth_algo": "paypal-auth-algo", "cert_url": "paypal-cert-url",
             "transmission_id": "paypal-transmission-id", "transmission_sig": "paypal-transmission-sig",
             "transmission_time": "paypal-transmission-time"}
    payload = {key: headers.get(name, "") for key, name in names.items()}
    if any(not isinstance(value, str) or not value or len(value) > 4096 for value in payload.values()):
        return False
    cert = urlsplit(payload["cert_url"])
    host = "api.paypal.com" if settings()[0] == "live" else "api.sandbox.paypal.com"
    if cert.scheme != "https" or cert.hostname != host or cert.username or cert.password:
        return False
    payload.update(webhook_id=webhook_id, webhook_event=event)
    return api("POST", "/v1/notifications/verify-webhook-signature", body=payload).get("verification_status") == "SUCCESS"
