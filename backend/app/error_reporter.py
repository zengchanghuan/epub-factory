"""
错误上报统一入口。

- 当前：写结构化日志（error_code 字段），供 CloudWatch/ELK 过滤
- 若设置 SENTRY_DSN 环境变量，自动向 Sentry 上报（FastAPI 集成在 main.py 初始化）
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
    """若配置了 SENTRY_DSN，向 Sentry 上报带结构化 tag 的错误事件。"""
    if not os.environ.get("SENTRY_DSN"):
        return
    try:
        import sentry_sdk  # type: ignore
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("error_code", error_code)
            if job_id:
                scope.set_tag("job_id", job_id)
            if trace_id:
                scope.set_tag("trace_id", trace_id)
            if context:
                for k, v in context.items():
                    scope.set_extra(k, v)
            sentry_sdk.capture_message(
                f"[{error_code}] {message}", level="error", scope=scope
            )
    except Exception:
        pass
