"""
翻译交付质检：把 chunk 审计、失败统计、元数据检查聚合成可展示报告。

第一版只使用规则型信号，不额外调用 LLM，保证稳定、便宜、可解释。
"""

from __future__ import annotations

import os
import html
import re
import zipfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag
from app.domain.translation_residual_policy import residual_category
from app.domain.epub_navigation_audit import audit_epub_navigation

from app.engine.chunk_extractor import (
    BLOCK_TAGS,
    should_skip_image_note_block,
    should_skip_reference_note_block,
    NON_TEXT_TAGS,
)


_NON_BODY_HEADINGS = {
    "index",
    "bibliography",
    "references",
    "sources",
    "works cited",
    "photo credits",
    "picture credits",
    "image credits",
    "illustration credits",
}

QA_RULES_VERSION = "20260918-confirmed-name-v6"


def max_free_retries() -> int:
    return int(os.environ.get("EPUB_TRANSLATION_MAX_FREE_RETRIES", "-1"))


def _flag(report: dict[str, Any], code: str, message: str) -> None:
    if code in report["flags"]:
        return
    report["flags"].append(code)
    report["checks"].append({"code": code, "message": message})


def _is_zh_target(target_lang: str | None) -> bool:
    return str(target_lang or "").lower().startswith("zh")


def _text_from_tag(tag: Tag) -> str:
    if not tag:
        return ""
    clone = BeautifulSoup(str(tag), "html.parser")
    for media in clone.find_all(list(NON_TEXT_TAGS)):
        if media.parent is not None:
            media.decompose()
    for br in clone.find_all("br"):
        br.replace_with(" ")
    text = clone.get_text("", strip=False)
    text = html.unescape(text or "")
    return re.sub(r"\s+", " ", text).strip()


def _normalize_heading(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip().lower()
    return re.sub(r"[\s:：.。]+$", "", text)


def _latin_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text or "")


def _latin_char_count(text: str) -> int:
    return sum(1 for ch in text or "" if ch.isascii() and ch.isalpha())


def _cjk_char_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", text or ""))


def _artifact_text_residual_category(text: str, block: Tag | None = None, preserved_terms=()) -> str:
    block_name = str(getattr(block, "name", "") or "").lower()
    class_tokens = {
        str(token).lower()
        for token in (block.get("class") or [])
    } if isinstance(block, Tag) else set()
    title_like = (
        block_name in {"h1", "h2", "h3", "h4", "h5", "h6"}
        or any(
            marker in token
            for token in class_tokens
            for marker in ("toc", "title", "heading", "head", "chapter")
        )
    )
    return residual_category(text, title_like=title_like, preserved_terms=preserved_terms)


def _is_non_body_document(member_name: str, soup: BeautifulSoup) -> bool:
    lower_name = (member_name or "").lower()
    if any(token in lower_name for token in ("nav", "toc", "copyright", "license", "colophon")):
        return True

    first_heading = ""
    for tag in soup.find_all(["h1", "h2", "h3"]):
        first_heading = _normalize_heading(_text_from_tag(tag))
        if first_heading:
            break
    return first_heading in _NON_BODY_HEADINGS


def audit_translated_epub_output(
    output_path: str | Path | None,
    *,
    target_lang: str | None = "zh-CN",
    bilingual: bool = False,
    sample_limit: int = 12,
    preserved_terms=(),
) -> dict[str, Any]:
    """Scan final EPUB text for obvious untranslated body blocks before delivery."""
    report: dict[str, Any] = {
        "rules_version": QA_RULES_VERSION,
        "status": "passed",
        "target_lang": target_lang or "",
        "html_files": 0,
        "non_body_files_skipped": 0,
        "text_blocks": 0,
        "checked_text_blocks": 0,
        "reference_note_blocks_skipped": 0,
        "residual_blocks": 0,
        "residual_categories": {},
        "samples": [],
    }
    if not _is_zh_target(target_lang):
        report["status"] = "skipped"
        report["reason"] = "target language is not Chinese"
        return report
    if not output_path or not Path(output_path).exists():
        report["status"] = "scan_error"
        report["reason"] = "output file is missing"
        return report

    max_residual = max(0, int(os.environ.get("EPUB_TRANSLATION_FINAL_QA_MAX_RESIDUAL_BLOCKS", "0")))
    sample_limit = max(0, min(int(sample_limit), 100))

    try:
        with zipfile.ZipFile(output_path) as zf:
            report.update(audit_epub_navigation(zf, preserved_terms, sample_limit))
            names = sorted(
                name for name in zf.namelist()
                if name.lower().endswith((".html", ".xhtml"))
            )
            for name in names:
                raw = zf.read(name).decode("utf-8", errors="replace")
                soup = BeautifulSoup(raw, "html.parser")
                report["html_files"] += 1
                if _is_non_body_document(name, soup):
                    report["non_body_files_skipped"] += 1
                    continue
                if bilingual:
                    # Only our paired source/translation markup is exempt. Unmarked
                    # English and English inside the translation still need auditing.
                    originals = [node for node in soup.select(".epub-original")
                                 if node.find_parent(class_="epub-original") is None]
                    for original in originals:
                        translated = original.find_next_sibling(class_="epub-translated")
                        if translated is None or not _text_from_tag(translated).strip():
                            report["residual_blocks"] += 1
                            categories = report["residual_categories"]
                            categories["missing_bilingual_translation"] = categories.get("missing_bilingual_translation", 0) + 1
                            if len(report["samples"]) < sample_limit:
                                report["samples"].append({
                                    "file": name,
                                    "category": "missing_bilingual_translation",
                                    "snippet": _text_from_tag(original)[:180],
                                })
                        original.decompose()
                for block in soup.find_all(BLOCK_TAGS):
                    if not isinstance(block, Tag) or block.find(BLOCK_TAGS):
                        continue
                    text = _text_from_tag(block)
                    if not text:
                        continue
                    report["text_blocks"] += 1
                    if should_skip_image_note_block(block):
                        continue
                    if should_skip_reference_note_block(block):
                        report["reference_note_blocks_skipped"] += 1
                        continue
                    report["checked_text_blocks"] += 1
                    category = _artifact_text_residual_category(text, block, preserved_terms)
                    if not category:
                        continue
                    report["residual_blocks"] += 1
                    categories = report["residual_categories"]
                    categories[category] = int(categories.get(category) or 0) + 1
                    if len(report["samples"]) < sample_limit:
                        report["samples"].append({
                            "file": name,
                            "tag": getattr(block, "name", ""),
                            "class": " ".join(block.get("class") or []),
                            "category": category,
                            "latin_chars": _latin_char_count(text),
                            "cjk_chars": _cjk_char_count(text),
                            "snippet": text[:180],
                        })
    except Exception as exc:
        report["status"] = "scan_error"
        report["reason"] = f"artifact audit failed: {exc}"
        return report

    if int(report["residual_blocks"] or 0) > max_residual:
        report["status"] = "failed"
        report["max_residual_blocks"] = max_residual
    if report.get("navigation_residual_labels") or report.get("navigation_broken_targets"):
        report["status"] = "failed"
    return report


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
    artifact_audit = stats.get("artifact_audit") if isinstance(stats.get("artifact_audit"), dict) else {}

    report: dict[str, Any] = {
        "rules_version": QA_RULES_VERSION,
        "status": "passed",
        "score": 100,
        "delivery_status": "pending",
        "can_deliver": False,
        "review_chunks": audit_warn,
        "categories_may_overlap": True,
        "summary": "翻译质检通过",
        "flags": [],
        "checks": [],
        "retryable": False,
        "free_retry_count": int(stats.get("free_retry_count") or 0),
        "max_free_retries": max_free_retries(),
        "translation_attempt": int(stats.get("translation_attempt") or 1),
    }

    if error_code == "TRANSLATION_PROVIDER_UNAVAILABLE" or stats.get("provider_blocked"):
        report.update(status="blocked", delivery_status="blocked", score=None,
                      retryable=(report["max_free_retries"] < 0
                                 or report["free_retry_count"] < report["max_free_retries"]),
                      summary=stats.get("last_error") or "模型服务暂不可用，译文缓存已保留")
        _flag(report, "provider_unavailable", report["summary"])
        return report

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

    artifact_residual = int(artifact_audit.get("residual_blocks") or 0)
    if artifact_audit.get("status") == "scan_error":
        _flag(report, "artifact_scan_error", "成品 EPUB 质检扫描失败，无法确认翻译完整性")
        report["score"] -= 80
    if artifact_residual:
        _flag(report, "artifact_untranslated_blocks", f"成品 EPUB 仍有 {artifact_residual} 个正文段落疑似未翻译")
        report["score"] -= min(80, 25 + artifact_residual * 2)
    navigation_residual = int(artifact_audit.get("navigation_residual_labels") or 0)
    broken_targets = int(artifact_audit.get("navigation_broken_targets") or 0)
    if navigation_residual:
        _flag(report, "navigation_untranslated", f"目录仍有 {navigation_residual} 个标题疑似未翻译")
        report["score"] -= min(30, navigation_residual * 3)
    if broken_targets:
        _flag(report, "navigation_broken", f"目录有 {broken_targets} 个失效链接或锚点")
        report["score"] -= min(50, broken_targets * 5)

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
        report["delivery_status"] = "failed"
        report["retryable"] = (
            report["max_free_retries"] < 0
            or report["free_retry_count"] < report["max_free_retries"]
        )
        report["summary"] = "；".join(item["message"] for item in report["checks"][:4])
    elif audit_warn:
        report["status"] = "warning"
        report["summary"] = f"{audit_warn} 个段落建议复核"

    if (report["status"] not in {"failed", "blocked"}
            and artifact_audit.get("status") in {"passed", "skipped"}
            and output_path and Path(output_path).is_file()):
        report.update(delivery_status="passed", can_deliver=True)

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
