import base64
import json
import logging
import os
from typing import Optional

from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient
from alipay.aop.api.domain.AlipayTradePagePayModel import AlipayTradePagePayModel
from alipay.aop.api.domain.AlipayTradePrecreateModel import AlipayTradePrecreateModel
from alipay.aop.api.domain.AlipayTradeQueryModel import AlipayTradeQueryModel
from alipay.aop.api.request.AlipayTradePagePayRequest import AlipayTradePagePayRequest
from alipay.aop.api.request.AlipayTradePrecreateRequest import AlipayTradePrecreateRequest
from alipay.aop.api.request.AlipayTradeQueryRequest import AlipayTradeQueryRequest
from alipay.aop.api.util.SignatureUtils import verify_with_rsa
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger("epub_factory.alipay")

_alipay_client: Optional[DefaultAlipayClient] = None
_alipay_public_key: Optional[str] = None


def _key_material(raw: str) -> bytes:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("密钥为空")
    normalized = raw.strip().replace("\\n", "\n")
    if "-----BEGIN " in normalized:
        return normalized.encode("ascii")
    return base64.b64decode("".join(normalized.split()), validate=True)


def normalize_alipay_public_key(raw: str) -> str:
    """Load a real RSA public key, never derive one from supplied private material."""
    try:
        material = _key_material(raw)
        if b"PRIVATE KEY" in material:
            raise ValueError("支付宝公钥字段不能填写私钥")
        if material.startswith(b"-----BEGIN "):
            key = serialization.load_pem_public_key(material)
        else:
            key = serialization.load_der_public_key(material)
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError("需要至少 2048 位的 RSA 公钥")
        return key.public_bytes(serialization.Encoding.PEM,
                                serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm):
        # Neither parsing errors nor the supplied material belong in logs. A bare
        # base64 private key used to be wrapped as PUBLIC KEY and fail only at pay time.
        raise ValueError("ALIPAY_PUBLIC_KEY 必须是至少 2048 位的支付宝平台 RSA 公钥，不能是私钥、应用公钥或证书") from None


def normalize_alipay_private_key(raw: str) -> str:
    """Canonicalize PKCS1/PKCS8 RSA app keys to the PKCS1 form required by the SDK."""
    try:
        material = _key_material(raw)
        if material.startswith(b"-----BEGIN "):
            key = serialization.load_pem_private_key(material, password=None)
        else:
            key = serialization.load_der_private_key(material, password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("需要至少 2048 位的 RSA 私钥")
        return key.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption()).decode("ascii")
    except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm):
        raise ValueError("ALIPAY_PRIVATE_KEY 必须是至少 2048 位且未加密的应用 RSA 私钥") from None


def init_alipay() -> bool:
    global _alipay_client, _alipay_public_key
    # A failed reconfiguration must not leave a previously initialized client active.
    _alipay_client = None
    _alipay_public_key = None
    app_id = os.environ.get("ALIPAY_APP_ID")
    app_private_key = os.environ.get("ALIPAY_PRIVATE_KEY")
    alipay_public_key = os.environ.get("ALIPAY_PUBLIC_KEY")

    if not all([app_id, app_private_key, alipay_public_key]):
        logger.warning("Alipay config missing, payments will be disabled unless SKIP_PAYMENT_CHECK=1")
        return False

    try:
        app_private_key = normalize_alipay_private_key(app_private_key)
        alipay_public_key = normalize_alipay_public_key(alipay_public_key)
        app_key = serialization.load_pem_private_key(app_private_key.encode("ascii"), password=None)
        platform_key = serialization.load_pem_public_key(alipay_public_key.encode("ascii"))
        if app_key.public_key().public_numbers() == platform_key.public_numbers():
            raise ValueError("ALIPAY_PUBLIC_KEY 填成了应用公钥，请改用支付宝平台公钥")
    except ValueError as exc:
        logger.error("Alipay 配置无效，支付已禁用：%s", exc)
        return False

    config = AlipayClientConfig()
    config.server_url = os.environ.get("ALIPAY_SERVER_URL", "https://openapi.alipay.com/gateway.do")
    config.app_id = app_id
    config.app_private_key = app_private_key
    config.alipay_public_key = alipay_public_key
    config.charset = "utf-8"
    config.sign_type = "RSA2"

    _alipay_client = DefaultAlipayClient(alipay_client_config=config)
    _alipay_public_key = alipay_public_key
    return True


def _verified_business_response(content: str, envelope: str) -> dict:
    """Parse only execute()'s successful return; signature exceptions stay failures.

    The pinned SDK verifies the raw response and returns the *inner* business JSON.
    Accept the envelope too for compatible SDK implementations, never exception text.
    """
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("Invalid Alipay response")
    data = payload.get(envelope, payload)
    if not isinstance(data, dict):
        raise ValueError("Invalid Alipay business response")
    return data


def create_alipay_page_pay(out_trade_no: str, total_amount: str, subject: str, return_url: str) -> str:
    """生成电脑网站支付链接 (GET)"""
    if not _alipay_client:
        raise ValueError("Alipay client not initialized")

    model = AlipayTradePagePayModel()
    model.out_trade_no = out_trade_no
    model.total_amount = total_amount
    model.subject = subject
    model.product_code = "FAST_INSTANT_TRADE_PAY"

    req = AlipayTradePagePayRequest(biz_model=model)
    notify_url = os.environ.get("ALIPAY_NOTIFY_URL", "https://fixepub.com/api/v2/webhooks/alipay")
    req.notify_url = notify_url
    req.return_url = return_url

    return _alipay_client.page_execute(req, http_method="GET")


def create_alipay_precreate(out_trade_no: str, total_amount: str, subject: str) -> str:
    """生成当面付(扫码支付) 二维码内容 url"""
    if not _alipay_client:
        raise ValueError("Alipay client not initialized")

    model = AlipayTradePrecreateModel()
    model.out_trade_no = out_trade_no
    model.total_amount = total_amount
    model.subject = subject

    req = AlipayTradePrecreateRequest(biz_model=model)
    notify_url = os.environ.get("ALIPAY_NOTIFY_URL", "https://fixepub.com/api/v2/webhooks/alipay")
    req.notify_url = notify_url

    response_content = _alipay_client.execute(req)
    if not response_content:
        raise ValueError("Alipay execute failed")
    resp = _verified_business_response(response_content, "alipay_trade_precreate_response")
    if resp.get("code") != "10000":
        raise ValueError(f"Alipay error: {resp.get('msg')} {resp.get('sub_msg')}")
    if resp.get("out_trade_no") not in (None, out_trade_no):
        raise ValueError("Alipay response order mismatch")
    qr_code = resp.get("qr_code")
    if not isinstance(qr_code, str) or not qr_code.strip():
        raise ValueError("Alipay response missing QR code")
    return qr_code


def query_alipay_trade(out_trade_no: str) -> Optional[str]:
    """查询可信交易状态；网络、验签、订单不存在等失败均返回 None（未知）。"""
    trade = query_verified_trade(out_trade_no)
    return trade.get("trade_status") if trade else None


def verify_alipay_notification(params: dict) -> bool:
    """验证异步通知签名，不修改调用方参数。"""
    if not _alipay_public_key:
        return False
    sign = params.get("sign")
    if not sign or params.get("sign_type", "RSA2") != "RSA2":
        return False
    message = "&".join(f"{key}={params[key]}" for key in sorted(params)
                       if key not in {"sign", "sign_type"} and params[key] not in (None, ""))
    try:
        return verify_with_rsa(_alipay_public_key, message.encode("utf-8"), sign)
    except Exception:
        logger.warning("Alipay notification signature verification failed")
        return False


def query_verified_trade(out_trade_no: str) -> Optional[dict]:
    """Accept only a successful SDK-verified response for this exact order."""
    if not _alipay_client:
        return None
    try:
        model = AlipayTradeQueryModel()
        model.out_trade_no = out_trade_no
        result = _alipay_client.execute(AlipayTradeQueryRequest(biz_model=model))
        data = _verified_business_response(result, "alipay_trade_query_response")
        if data.get("code") != "10000" or data.get("out_trade_no") != out_trade_no:
            return None
        return {key: data.get(key) for key in
                ("out_trade_no", "trade_status", "total_amount", "trade_no")}
    except Exception:
        # SDK signature errors may embed a complete response. Never interpret that
        # unverified payload as payment evidence or write it to application logs.
        logger.warning("Verified Alipay payment query unavailable", extra={"job_id": out_trade_no})
        return None
