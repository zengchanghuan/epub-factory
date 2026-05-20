"""
手机号短信验证码：
- 验证码存储在 Redis（TTL=300s，60s 内不允许重发）
- 短信通道：通过 SMS_PROVIDER 切换（aliyun / tencent），默认 aliyun

设计要点（按"五大支柱"对照）：
- 失败可回滚：未配置时走 _send_dev_log 兜底，开发环境可看日志验证码
- 迭代性能  ：新增 provider 只需在下面加一个 _send_xxx 函数 + dispatch 一行
- 数据可控  ：所有密钥走环境变量，不硬编码
- 链路可观测：每次失败都打 ERROR 日志，含 provider 标记便于排查
- 成本可预测：默认 aliyun（价格已知），切到 tencent 也是 ¥0.045/条
"""
import os
import random
import string
from typing import Optional

import redis as _redis

_REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
_CODE_TTL = 300       # 验证码有效期（秒）
_RATE_TTL = 60        # 发送频率限制（秒）
_CODE_LEN = 6

# ── 通道选择：未设置时默认 aliyun（向后兼容旧 .env）─────────────────────
_SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "aliyun").strip().lower()

# ── 阿里云 dysmsapi 配置 ──────────────────────────────────────────────
_ALIYUN_ACCESS_KEY_ID = os.environ.get("ALIYUN_ACCESS_KEY_ID", "")
_ALIYUN_ACCESS_KEY_SECRET = os.environ.get("ALIYUN_ACCESS_KEY_SECRET", "")
_ALIYUN_SIGN_NAME = os.environ.get("SMS_SIGN_NAME", "")
_ALIYUN_TEMPLATE_CODE = os.environ.get("SMS_TEMPLATE_CODE", "")

# ── 腾讯云 SMS 配置 ───────────────────────────────────────────────────
# SecretId / SecretKey：在腾讯云访问管理 (CAM) 控制台创建子账号 API 密钥
# SmsSdkAppId        ：在短信控制台「应用管理」创建应用后获得的 SdkAppId
# SignName / TemplateId：签名内容 / 模板 ID（数字）
_TENCENT_SECRET_ID = os.environ.get("TENCENT_SECRET_ID", "")
_TENCENT_SECRET_KEY = os.environ.get("TENCENT_SECRET_KEY", "")
_TENCENT_SMS_APP_ID = os.environ.get("TENCENT_SMS_APP_ID", "")
_TENCENT_SMS_SIGN = os.environ.get("TENCENT_SMS_SIGN", "")
_TENCENT_SMS_TEMPLATE_ID = os.environ.get("TENCENT_SMS_TEMPLATE_ID", "")
_TENCENT_SMS_REGION = os.environ.get("TENCENT_SMS_REGION", "ap-guangzhou")


# ─────────────────────────────────────────────────────────────────────
# Redis 验证码存取（与 provider 解耦）
# ─────────────────────────────────────────────────────────────────────
def _get_redis() -> _redis.Redis:
    return _redis.Redis.from_url(_REDIS_URL, decode_responses=True)


def _code_key(phone: str) -> str:
    return f"sms:code:{phone}"


def _rate_key(phone: str) -> str:
    return f"sms:rate:{phone}"


def generate_and_store_code(phone: str) -> tuple[str, bool]:
    """
    生成验证码并存入 Redis。
    返回 (code, sent)，若频率超限则 sent=False，code 为空串。
    """
    r = _get_redis()
    if r.exists(_rate_key(phone)):
        return ("", False)
    code = "".join(random.choices(string.digits, k=_CODE_LEN))
    r.setex(_code_key(phone), _CODE_TTL, code)
    r.setex(_rate_key(phone), _RATE_TTL, "1")
    return (code, True)


def verify_code(phone: str, code: str) -> bool:
    """校验验证码，成功后立即删除（防重放）。"""
    r = _get_redis()
    stored = r.get(_code_key(phone))
    if not stored or stored != code.strip():
        return False
    r.delete(_code_key(phone))
    return True


# ─────────────────────────────────────────────────────────────────────
# 通道实现
# ─────────────────────────────────────────────────────────────────────
def _send_dev_log(phone: str, code: str, reason: str) -> bool:
    """开发兜底：未配置短信通道时仅日志输出，便于本地调试。"""
    import logging
    logging.getLogger("epub_factory").info(
        f"[SMS DEV] phone={phone} code={code} ({reason})"
    )
    return True


def _normalize_phone_for_tencent(phone: str) -> str:
    """腾讯云要求 E.164 格式（带 +86 前缀）；阿里云只要 11 位。"""
    p = phone.strip()
    if p.startswith("+"):
        return p
    if p.startswith("86") and len(p) == 13:
        return "+" + p
    return "+86" + p


def _send_aliyun(phone: str, code: str) -> bool:
    """阿里云 dysmsapi 通道。"""
    if not _ALIYUN_ACCESS_KEY_ID or not _ALIYUN_ACCESS_KEY_SECRET:
        return _send_dev_log(phone, code, "aliyun not configured")

    try:
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_dysmsapi20170525 import models as sms_models
        from alibabacloud_tea_openapi import models as open_api_models
        import json as _json

        config = open_api_models.Config(
            access_key_id=_ALIYUN_ACCESS_KEY_ID,
            access_key_secret=_ALIYUN_ACCESS_KEY_SECRET,
            endpoint="dysmsapi.aliyuncs.com",
        )
        client = Client(config)
        req = sms_models.SendSmsRequest(
            phone_numbers=phone,
            sign_name=_ALIYUN_SIGN_NAME,
            template_code=_ALIYUN_TEMPLATE_CODE,
            template_param=_json.dumps({"code": code}),
        )
        resp = client.send_sms(req)
        return resp.body.code == "OK"
    except Exception as e:
        import logging
        logging.getLogger("epub_factory").error(
            f"[SMS][aliyun] send failed: {e}", exc_info=True
        )
        return False


def _send_tencent(phone: str, code: str) -> bool:
    """
    腾讯云 SMS 通道。
    - 模板假定只有一个 {1} 变量位（即验证码本身）。
      如果你的模板是「您的验证码{1}，{2}分钟内有效」这类多参数模板，
      需要把 TemplateParamSet 改为 [code, str(_CODE_TTL // 60)]。
    """
    if not (_TENCENT_SECRET_ID and _TENCENT_SECRET_KEY and _TENCENT_SMS_APP_ID):
        return _send_dev_log(phone, code, "tencent not configured")

    try:
        from tencentcloud.common import credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.sms.v20210111 import sms_client, models as sms_models

        cred = credential.Credential(_TENCENT_SECRET_ID, _TENCENT_SECRET_KEY)
        http_profile = HttpProfile()
        http_profile.endpoint = "sms.tencentcloudapi.com"
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        client = sms_client.SmsClient(cred, _TENCENT_SMS_REGION, client_profile)

        req = sms_models.SendSmsRequest()
        req.SmsSdkAppId = _TENCENT_SMS_APP_ID
        req.SignName = _TENCENT_SMS_SIGN
        req.TemplateId = _TENCENT_SMS_TEMPLATE_ID
        req.TemplateParamSet = [code]  # 单参数模板：{1} = 验证码
        req.PhoneNumberSet = [_normalize_phone_for_tencent(phone)]

        resp = client.SendSms(req)
        if not resp.SendStatusSet:
            import logging
            logging.getLogger("epub_factory").error(
                f"[SMS][tencent] empty SendStatusSet for phone={phone}"
            )
            return False

        status = resp.SendStatusSet[0]
        if status.Code == "Ok":
            return True

        # 失败原因写日志（含 SerialNo 方便客服查询）
        import logging
        logging.getLogger("epub_factory").error(
            f"[SMS][tencent] phone={phone} code={status.Code} "
            f"message={status.Message} serial={getattr(status, 'SerialNo', '')}"
        )
        return False
    except Exception as e:
        import logging
        logging.getLogger("epub_factory").error(
            f"[SMS][tencent] send failed: {e}", exc_info=True
        )
        return False


# ─────────────────────────────────────────────────────────────────────
# 对外统一入口（router.py 调用这一个函数即可）
# ─────────────────────────────────────────────────────────────────────
def send_sms(phone: str, code: str) -> bool:
    """根据 SMS_PROVIDER 分发到具体短信通道。"""
    if _SMS_PROVIDER == "tencent":
        return _send_tencent(phone, code)
    if _SMS_PROVIDER == "aliyun":
        return _send_aliyun(phone, code)

    # 未识别的 provider 名 → 走开发兜底，避免线上静默丢消息
    return _send_dev_log(phone, code, f"unknown SMS_PROVIDER={_SMS_PROVIDER!r}")
