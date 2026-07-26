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
import unicodedata
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
    likely_untranslated: bool = False

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
            "likely_untranslated": self.likely_untranslated,
        }


def _text(html: str) -> str:
    # 不人为在相邻内联标签间插入空格。像
    # C<small>URRICULUM</small> 这样的排版应还原为 CURRICULUM，
    # 否则会逃过英文残留检测并制造术语误报。
    soup = BeautifulSoup(html or "", "html.parser")
    for br in soup.find_all("br"):
        br.replace_with(" ")
    raw = soup.get_text("", strip=False)
    return re.sub(r"\s+", " ", raw).strip()


def _extract_inner_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    first = soup.find()
    if first and first.name in BLOCK_TAGS:
        return "".join(str(c) for c in first.contents)
    return html or ""


def _tag_counter(html: str) -> Counter[str]:
    inner_html = _extract_inner_html(html)
    if "<" not in inner_html or ">" not in inner_html:
        return Counter()
    soup = BeautifulSoup(inner_html, "html.parser")
    return Counter(tag.name for tag in soup.find_all(True) if isinstance(tag, Tag))


def _numbers(text: str) -> list[str]:
    # 覆盖 3, 3.14, 1,000, 2024-06-25, 12:30 等常见形态。
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = normalized.translate(str.maketrans({
        "–": "-",
        "—": "-",
        "−": "-",
        "：": ":",
        "／": "/",
        "，": ",",
        "．": ".",
    }))
    return re.findall(r"\d+(?:[,\.\-:/]\d+)*", normalized)


def _latin_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text or "")


def _latin_char_count(text: str) -> int:
    return sum(1 for ch in text or "" if ch.isascii() and ch.isalpha())


def _cjk_char_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", text or ""))


def _likely_untranslated_english(source_text: str, translated_text: str) -> bool:
    """保守识别英文源段落在中文译文中大量原样残留的情况。"""
    source_words = _latin_words(source_text)
    if not translated_text:
        return False

    normalize = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()
    if (
        normalize(source_text) == normalize(translated_text)
        and len(source_words) >= 2
        and _latin_char_count(source_text) >= 12
    ):
        return True
    if len(source_words) < 6:
        return False

    translated_words = _latin_words(translated_text)
    translated_latin = _latin_char_count(translated_text)
    translated_cjk = _cjk_char_count(translated_text)

    if (
        len(translated_words) >= max(6, int(len(source_words) * 0.7))
        and translated_cjk < max(6, int(translated_latin * 0.15))
    ):
        return True

    if (
        translated_cjk > 0
        and len(translated_words) >= 12
        and translated_latin > max(120, translated_cjk * 2.5)
    ):
        return True

    return False


def _set_risk(current: str, new: str) -> str:
    order = {"ok": 0, "warn": 1, "fail": 2}
    return new if order[new] > order[current] else current


def _term_spans(term: str, text: str) -> list[tuple[int, int]]:
    if not term or not text:
        return []
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9\s'’.\-]*", term):
        pattern = r"\b" + re.escape(term) + r"\b"
        return [m.span() for m in re.finditer(pattern, text, flags=re.IGNORECASE)]
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = text.find(term, start)
        if index < 0:
            break
        spans.append((index, index + len(term)))
        start = index + max(1, len(term))
    return spans


def _relevant_glossary_terms(
    glossary: dict[str, str],
    source_text: str,
) -> list[tuple[str, str]]:
    """最长短语优先，避免 French 与 French Revolution 同时制造告警。"""
    candidates: list[tuple[int, int, str, str]] = []
    for src, dst in glossary.items():
        if not src or not dst:
            continue
        for start, end in _term_spans(src, source_text):
            candidates.append((start, end, src, dst))
    candidates.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))

    selected: list[tuple[int, int, str, str]] = []
    for candidate in candidates:
        start, end, _src, _dst = candidate
        if any(start < kept_end and end > kept_start for kept_start, kept_end, *_ in selected):
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: item[0])
    return [(src, dst) for _start, _end, src, dst in selected]


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

    if _likely_untranslated_english(source_text, translated_text):
        audit.likely_untranslated = True
        audit.flags.append("likely_untranslated")
        audit.risk_level = _set_risk(audit.risk_level, "fail")

    if error_like_checker and error_like_checker(translated_html):
        audit.error_like_response = True
        audit.flags.append("error_like_response")
        audit.risk_level = _set_risk(audit.risk_level, "fail")

    # 长文本异常过短很可能是漏译/截断；短标题不做长度告警，避免噪音。
    if source_len >= 40 and translated_len > 0 and length_ratio < 0.25:
        audit.flags.append("suspiciously_short_translation")
        audit.risk_level = _set_risk(audit.risk_level, "warn")

    src_numbers = Counter(_numbers(source_text))
    if src_numbers:
        translated_numbers = Counter(_numbers(translated_text))
        missing = list((src_numbers - translated_numbers).elements())
        if missing:
            audit.numbers_missing = missing
            audit.flags.append("numbers_missing")
            audit.risk_level = _set_risk(audit.risk_level, "warn")

    glossary = glossary or {}
    missing_terms: list[str] = []
    for src, dst in _relevant_glossary_terms(glossary, source_text):
        if dst not in translated_text:
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
