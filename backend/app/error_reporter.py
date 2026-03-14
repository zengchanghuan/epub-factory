"""
错误上报统一入口。

- 当前：写结构化日志（error_code 字段），供 CloudWatch/ELK 过滤
- 预留：设置 SENTRY_DSN 环境变量后自动向 Sentry 上报
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("epub_factory")


def report_error(
    error_code: str,
    message: str,
    job_id: str = "",
    trace_id: str = "",
    context: Optional[dict] = None,
) -> None:
    """统一错误上报：写结构化日志，并可选上报 Sentry。"""
    logger.error(
        "job_error",
        extra={
            "job_id": job_id,
            "trace_id": trace_id,
            "error_code": error_code,
            "error_message": message,
            **(context or {}),
        },
    )
    _sentry_capture(error_code, message, job_id, trace_id, context)


def _sentry_capture(
    error_code: str,
    message: str,
    job_id: str,
    trace_id: str,
    context: Optional[dict],
) -> None:
    """若配置了 SENTRY_DSN，自动向 Sentry 上报错误事件。"""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk  # type: ignore
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("error_code", error_code)
            scope.set_extra("job_id", job_id)
            scope.set_extra("trace_id", trace_id)
            scope.set_extra("context", context or {})
            sentry_sdk.capture_message(
                f"[{error_code}] {message}", level="error"
            )
    except Exception:
        pass
