"""
翻译交付质检：把 chunk 审计、失败统计、元数据检查聚合成可展示报告。

第一版只使用规则型信号，不额外调用 LLM，保证稳定、便宜、可解释。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def max_free_retries() -> int:
    return int(os.environ.get("EPUB_TRANSLATION_MAX_FREE_RETRIES", "-1"))


def _flag(report: dict[str, Any], code: str, message: str) -> None:
    if code not in report["flags"]:
        report["flags"].append(code)
    report["checks"].append({"code": code, "message": message})


def build_translation_qa_report(
    *,
    translation_stats: dict[str, Any] | None,
    output_path: str | Path | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    stats = translation_stats or {}
    failed = int(stats.get("failed_chunks") or 0)
    total = int(stats.get("total_chunks") or 0)
    audit_failed = int(stats.get("audit_failed_chunks") or 0)
    audit_warn = int(stats.get("audit_warn_chunks") or 0)
    flags_count = stats.get("audit_flags_count") or {}
    title_original = (stats.get("book_title_original") or "").strip()
    title_translated = (stats.get("book_title_translated") or "").strip()

    report: dict[str, Any] = {
        "status": "passed",
        "score": 100,
        "summary": "翻译质检通过",
        "flags": [],
        "checks": [],
        "retryable": False,
        "free_retry_count": int(stats.get("free_retry_count") or 0),
        "max_free_retries": max_free_retries(),
        "translation_attempt": int(stats.get("translation_attempt") or 1),
    }

    if failed:
        _flag(report, "failed_chunks", f"{failed} 个段落翻译失败")
        report["score"] -= min(80, 20 + failed * 2)

    likely_untranslated = int(flags_count.get("likely_untranslated") or 0)
    if likely_untranslated:
        _flag(report, "likely_untranslated", f"{likely_untranslated} 个段落疑似仍为英文原文")
        report["score"] -= min(60, 20 + likely_untranslated * 3)

    if audit_failed:
        _flag(report, "audit_failed_chunks", f"{audit_failed} 个段落质检失败")
        report["score"] -= min(50, audit_failed * 2)

    if title_original and title_translated and title_original == title_translated:
        _flag(report, "title_untranslated", "书名元数据未翻译")
        report["score"] -= 15

    if error_code == "PARTIAL_TRANSLATION":
        _flag(report, "partial_translation", "翻译结果不完整")

    if output_path and not Path(output_path).exists():
        _flag(report, "output_missing", "输出文件不存在")
        report["score"] -= 60

    report["score"] = max(0, int(report["score"]))
    if report["flags"]:
        report["status"] = "failed"
        report["retryable"] = (
            report["max_free_retries"] < 0
            or report["free_retry_count"] < report["max_free_retries"]
        )
        report["summary"] = "；".join(item["message"] for item in report["checks"][:4])
    elif audit_warn:
        report["status"] = "warning"
        report["summary"] = f"{audit_warn} 个段落建议复核"

    return report


def attach_translation_qa_report(
    translation_stats: dict[str, Any] | None,
    *,
    output_path: str | Path | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    stats = dict(translation_stats or {})
    stats["qa_report"] = build_translation_qa_report(
        translation_stats=stats,
        output_path=output_path,
        error_code=error_code,
    )
    return stats
