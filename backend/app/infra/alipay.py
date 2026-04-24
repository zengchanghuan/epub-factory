import logging
import os
from typing import Optional

from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient
from alipay.aop.api.domain.AlipayTradePagePayModel import AlipayTradePagePayModel
from alipay.aop.api.domain.AlipayTradePrecreateModel import AlipayTradePrecreateModel
from alipay.aop.api.request.AlipayTradePagePayRequest import AlipayTradePagePayRequest
from alipay.aop.api.request.AlipayTradePrecreateRequest import AlipayTradePrecreateRequest
from alipay.aop.api.util.SignatureUtils import verify_with_rsa

logger = logging.getLogger("epub_factory.alipay")

_alipay_client: Optional[DefaultAlipayClient] = None
_alipay_public_key: Optional[str] = None

def init_alipay() -> bool:
    global _alipay_client, _alipay_public_key
    app_id = os.environ.get("ALIPAY_APP_ID")
    app_private_key = os.environ.get("ALIPAY_PRIVATE_KEY")
    alipay_public_key = os.environ.get("ALIPAY_PUBLIC_KEY")

    if not all([app_id, app_private_key, alipay_public_key]):
        logger.warning("Alipay config missing, payments will be disabled unless SKIP_PAYMENT_CHECK=1")
        return False

    # Normalize keys if they don't have header/footer
    if "BEGIN" not in app_private_key:
        app_private_key = f"-----BEGIN RSA PRIVATE KEY-----\n{app_private_key}\n-----END RSA PRIVATE KEY-----"
    if "BEGIN" not in alipay_public_key:
        alipay_public_key = f"-----BEGIN PUBLIC KEY-----\n{alipay_public_key}\n-----END PUBLIC KEY-----"

    _alipay_public_key = alipay_public_key

    config = AlipayClientConfig()
    config.server_url = os.environ.get("ALIPAY_SERVER_URL", "https://openapi.alipay.com/gateway.do")
    config.app_id = app_id
    config.app_private_key = app_private_key
    config.alipay_public_key = alipay_public_key
    config.charset = "utf-8"
    config.sign_type = "RSA2"

    _alipay_client = DefaultAlipayClient(alipay_client_config=config)
    return True

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

    pay_url = _alipay_client.page_execute(req, http_method="GET")
    return pay_url

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
    
    import json
    res_dict = json.loads(response_content)
    # The response dict is like: {'alipay_trade_precreate_response': {'code': '10000', 'qr_code': 'https://...'}}
    resp = res_dict.get("alipay_trade_precreate_response", {})
    if resp.get("code") != "10000":
        raise ValueError(f"Alipay error: {resp.get('msg')} {resp.get('sub_msg')}")
    
    return resp.get("qr_code")

def verify_alipay_notification(params: dict) -> bool:
    """验证异步通知签名"""
    if not _alipay_public_key:
        return False
    
    sign = params.pop("sign", None)
    params.pop("sign_type", None)
    if not sign:
        return False
    
    # 按照支付宝规则排序拼接
    unsigned_items = []
    for k in sorted(params.keys()):
        v = params[k]
        if v:
            unsigned_items.append(f"{k}={v}")
    message = "&".join(unsigned_items)
    
    try:
        return verify_with_rsa(_alipay_public_key.encode("utf-8"), message.encode("utf-8"), sign)
    except Exception as e:
        logger.error(f"Alipay signature verification failed: {e}")
        return False
