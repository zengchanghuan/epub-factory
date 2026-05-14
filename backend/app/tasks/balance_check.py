"""
大模型余额监控 Celery 任务。

每天定时（默认 08:00 Asia/Shanghai）查询 DeepSeek 账户余额，
余额低于阈值时写 ERROR 日志 → 触发 CLS 告警规则 → 收短信通知。

环境变量：
  OPENAI_API_KEY          DeepSeek API Key（沿用现有配置）
  OPENAI_BASE_URL         API 地址（默认 https://api.deepseek.com/v1）
  BALANCE_WARN_CNY        余额告警阈值（元，默认 10）
  BALANCE_CHECK_PROVIDER  当前提供商标识，用于日志（默认 deepseek）
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from app.infra.celery_app import celery_app

logger = logging.getLogger("epub_factory.balance")

_WARN_THRESHOLD = float(os.environ.get("BALANCE_WARN_CNY", "10"))
_PROVIDER = os.environ.get("BALANCE_CHECK_PROVIDER", "deepseek")


@celery_app.task(name="infra.check_balance", bind=True, max_retries=0)
def check_balance(self) -> dict:
    """查询大模型账户余额，余额不足时记 ERROR 日志触发 CLS 告警。"""
    balance = _fetch_deepseek_balance()

    if balance is None:
        logger.error(
            "balance_check: 余额查询失败，请检查 API Key 或网络",
            extra={"error_code": "BALANCE_QUERY_FAILED", "provider": _PROVIDER},
        )
        return {"provider": _PROVIDER, "balance_cny": None, "status": "error"}

    logger.info(
        "balance_check: ok",
        extra={"provider": _PROVIDER, "balance_cny": balance},
    )

    if balance < _WARN_THRESHOLD:
        logger.error(
            f"balance_check: 余额不足 ¥{balance:.2f}，请及时充值（阈值 ¥{_WARN_THRESHOLD:.0f}）",
            extra={
                "error_code": "BALANCE_LOW",
                "provider": _PROVIDER,
                "balance_cny": balance,
                "threshold_cny": _WARN_THRESHOLD,
            },
        )
        return {"provider": _PROVIDER, "balance_cny": balance, "status": "low"}

    return {"provider": _PROVIDER, "balance_cny": balance, "status": "ok"}


def _fetch_deepseek_balance() -> Optional[float]:
    """
    调 DeepSeek 余额查询接口。
    文档：https://api-docs.deepseek.com/zh-cn/api/get-user-balance
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    # 去掉 /v1 后缀，余额接口在 /user/balance
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]

    url = f"{base}/user/balance"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # 取充值余额（topped_up_balance）+ 赠送余额（granted_balance）
        infos = data.get("balance_infos", [])
        cny_info = next((b for b in infos if b.get("currency") == "CNY"), None)
        if not cny_info:
            return None
        return float(cny_info.get("total_balance") or 0)
    except Exception as e:
        logger.warning(f"DeepSeek balance fetch failed: {e}")
        return None
