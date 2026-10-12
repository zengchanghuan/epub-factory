"""
任务状态判定：明确 completed / partial_completed / failed 边界，杜绝假成功。

规则（与设计文档 12.3 一致）：
- completed：结果可下载且可信（打包成功 + EpubCheck 通过，且非“部分翻译失败”的告警）
- partial translation：质检失败，保持 failed，不允许下载
- failed：全翻译失败、打包失败、校验失败等不可交付
"""

from typing import Optional, Tuple

from app.models import ConversionResult, JobStatus


# 校验未通过时使用的错误码，供前端/API 提示
EPUB_VALIDATION_FAILED = "EPUB_VALIDATION_FAILED"


def resolve_after_conversion(result: ConversionResult) -> Tuple[JobStatus, str, Optional[str]]:
    """
    根据转换结果决定最终任务状态与错误码。

    :param result: 转换器返回的 ConversionResult
    :return: (JobStatus, message, error_code)
    """
    code = result.error_code
    if not getattr(result, "validation_passed", False) or code:
        return (
            JobStatus.failed,
            result.message or "EPUB 校验未通过，结果不可交付",
            code or EPUB_VALIDATION_FAILED,
        )
    return (JobStatus.success, result.message or "转换成功", code)
