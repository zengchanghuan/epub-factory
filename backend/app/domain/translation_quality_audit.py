"""
规则型翻译可信度审计。

目标不是替代人工审稿，而是在不增加 LLM 成本的前提下，标出最值得复核的 chunk：
- 模型拒答/错误响应
- 译文异常过短或为空
- 数字丢失
- glossary 译名未体现
- HTML 内联标签被破坏
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from bs4 import BeautifulSoup, Tag


BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}


@dataclass
class TranslationQualityAudit:
    source_text: str
    translated_text: str
    length_ratio: float
    risk_level: str = "ok"  # ok | warn | fail
    flags: list[str] = field(default_factory=list)
    numbers_missing: list[str] = field(default_factory=list)
    latin_terms_missing: list[str] = field(default_factory=list)
    html_tag_mismatch: bool = False
    error_like_response: bool = False

    def to_dict(self) -> dict:
        return {
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "length_ratio": self.length_ratio,
            "risk_level": self.risk_level,
            "flags": self.flags,
            "numbers_missing": self.numbers_missing,
            "latin_terms_missing": self.latin_terms_missing,
            "html_tag_mismatch": self.html_tag_mismatch,
            "error_like_response": self.error_like_response,
        }


def _text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def _extract_inner_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    first = soup.find()
    if first and first.name in BLOCK_TAGS:
        return "".join(str(c) for c in first.contents)
    return html or ""


def _tag_counter(html: str) -> Counter[str]:
    soup = BeautifulSoup(_extract_inner_html(html), "html.parser")
    return Counter(tag.name for tag in soup.find_all(True) if isinstance(tag, Tag))


def _numbers(text: str) -> list[str]:
    # 覆盖 3, 3.14, 1,000, 2024-06-25, 12:30 等常见形态。
    return re.findall(r"\d+(?:[,\.\-:/]\d+)*", text or "")


def _set_risk(current: str, new: str) -> str:
    order = {"ok": 0, "warn": 1, "fail": 2}
    return new if order[new] > order[current] else current


def audit_translation_chunk(
    *,
    original_html: str,
    translated_html: str,
    glossary: dict[str, str] | None = None,
    error_like_checker: Callable[[str], bool] | None = None,
) -> TranslationQualityAudit:
    """对单个 chunk 做规则型可信度审计。"""
    source_text = _text(original_html)
    translated_text = _text(translated_html)
    source_len = len(source_text)
    translated_len = len(translated_text)
    length_ratio = round(translated_len / source_len, 3) if source_len else 1.0

    audit = TranslationQualityAudit(
        source_text=source_text,
        translated_text=translated_text,
        length_ratio=length_ratio,
    )

    if source_text and not translated_text:
        audit.flags.append("empty_translation")
        audit.risk_level = _set_risk(audit.risk_level, "fail")

    if error_like_checker and error_like_checker(translated_html):
        audit.error_like_response = True
        audit.flags.append("error_like_response")
        audit.risk_level = _set_risk(audit.risk_level, "fail")

    # 长文本异常过短很可能是漏译/截断；短标题不做长度告警，避免噪音。
    if source_len >= 40 and translated_len > 0 and length_ratio < 0.25:
        audit.flags.append("suspiciously_short_translation")
        audit.risk_level = _set_risk(audit.risk_level, "warn")

    src_numbers = _numbers(source_text)
    if src_numbers:
        missing = [n for n in src_numbers if n not in translated_text]
        if missing:
            audit.numbers_missing = missing
            audit.flags.append("numbers_missing")
            audit.risk_level = _set_risk(audit.risk_level, "warn")

    glossary = glossary or {}
    missing_terms: list[str] = []
    for src, dst in glossary.items():
        if not src or not dst:
            continue
        if src in source_text and dst not in translated_text:
            missing_terms.append(src)
    if missing_terms:
        audit.latin_terms_missing = missing_terms
        audit.flags.append("glossary_terms_missing")
        audit.risk_level = _set_risk(audit.risk_level, "warn")

    if _tag_counter(original_html) != _tag_counter(translated_html):
        audit.html_tag_mismatch = True
        audit.flags.append("html_tag_mismatch")
        audit.risk_level = _set_risk(audit.risk_level, "fail")

    return audit
