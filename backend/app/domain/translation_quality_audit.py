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
from app.domain.translation_residual_policy import residual_category
from app.domain.translation_numeric_audit import missing_numeric_facts
from app.engine.chunk_extractor import NON_TEXT_TAGS, media_subtrees


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
    critical_markers_missing: list[str] = field(default_factory=list)
    source_sentence_count: int = 0
    translated_sentence_count: int = 0
    sentence_alignment_ratio: float = 1.0

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
            "critical_markers_missing": self.critical_markers_missing,
            "source_sentence_count": self.source_sentence_count,
            "translated_sentence_count": self.translated_sentence_count,
            "sentence_alignment_ratio": self.sentence_alignment_ratio,
        }


def _text(html: str) -> str:
    # 不人为在相邻内联标签间插入空格。像
    # C<small>URRICULUM</small> 这样的排版应还原为 CURRICULUM，
    # 否则会逃过英文残留检测并制造术语误报。
    soup = BeautifulSoup(html or "", "html.parser")
    for media in soup.find_all(list(NON_TEXT_TAGS)):
        if media.parent is not None:
            media.decompose()
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


def _numeric_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for note in soup.find_all(lambda tag: tag.name == 'sup' or 'noteref' in str(tag.get('epub:type') or '').split()):
        note.insert_before(' ')
        note.insert_after(' ')
    return _text(str(soup))


def _latin_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text or "")


def _latin_char_count(text: str) -> int:
    return sum(1 for ch in text or "" if ch.isascii() and ch.isalpha())


def _cjk_char_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", text or ""))


def _likely_untranslated_english(source_text: str, translated_text: str, preserved_terms=(), title_like=False) -> bool:
    """保守识别英文源段落在中文译文中大量原样残留的情况。"""
    return bool(residual_category(translated_text, source_text=source_text,
                                  preserved_terms=preserved_terms, title_like=title_like))


def _set_risk(current: str, new: str) -> str:
    order = {"ok": 0, "warn": 1, "fail": 2}
    return new if order[new] > order[current] else current


_ENGLISH_TO_CHINESE_MARKERS = (
    (
        "negation",
        re.compile(r"\b(?:not|never|no|none|neither|nor|without|cannot|can't|didn't|doesn't|isn't|wasn't|won't)\b", re.I),
        re.compile(r"(?:不|未|无|非|没|莫|否|绝不|从未|不能|无法|并非|并不)"),
    ),
    (
        "causal",
        re.compile(r"\b(?:because|therefore|thus|consequently|hence|due to|as a result)\b", re.I),
        re.compile(r"(?:因|由|缘于|源于|所以|故而|故|从而|于是|结果)"),
    ),
    (
        "contrast",
        re.compile(r"\b(?:however|nevertheless|nonetheless|although|though|despite|but|whereas|yet)\b", re.I),
        re.compile(r"(?:但是|但|然而|不过|却|尽管|虽然|仍然|反之|而)"),
    ),
)


def _missing_critical_markers(source_text: str, translated_text: str) -> list[str]:
    """Conservative English→Chinese relation check used only as a review signal."""
    if _cjk_char_count(translated_text) < 3:
        return []
    missing: list[str] = []
    # Fixed phrases are not causal/adversative relations. Keep genuinely
    # adversative "yet/but" elsewhere instead of whitelisting whole paragraphs.
    relation_text = source_text.replace('’', "'")
    relation_text = re.sub(r'\b(?:better yet|yet again|thus far|all but)\b', '', relation_text, flags=re.I)
    relation_text = re.sub(r'\b(not\s+only\b[^.!?;]{0,120})\bbut\b', r'\1', relation_text, flags=re.I)
    relation_text = re.sub(r'\bbut\s+also\b', '', relation_text, flags=re.I)
    relation_text = re.sub(r'\bnot\s+yet\b', 'not', relation_text, flags=re.I)
    relation_text = re.sub(r"\b((?:[A-Za-z]+n't|not|never)\b[^.!?;]{0,80})\byet(?=\s*[,.!?;]|\s*$)", r'\1', relation_text, flags=re.I)
    for label, source_pattern, translated_pattern in _ENGLISH_TO_CHINESE_MARKERS:
        if source_pattern.search(relation_text) and not translated_pattern.search(translated_text):
            missing.append(label)
    return missing


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


def _sentence_count(text: str) -> int:
    text = re.sub(r'\b(?:a\.m\.|p\.m\.|e\.g\.|i\.e\.)', lambda m: m.group().replace('.', ''), text or '', flags=re.I)
    text = re.sub(r'\b[A-Z]\.(?=\s|[A-Z])', lambda m: m.group()[:-1], text)
    text = re.sub(r'(?:\.\s*){3,}|…+', ' ', text)
    text = re.sub(r'(?<=\d)\.(?=\d)', '', text)
    parts = [
        item.strip()
        for item in re.split(r"(?<=[.!?。！？；;])\s*", text or "")
        if item.strip()
    ]
    return len(parts) if parts else (1 if str(text or "").strip() else 0)


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
    preserved_terms=(),
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
    audit.source_sentence_count = _sentence_count(source_text)
    audit.translated_sentence_count = _sentence_count(translated_text)
    audit.sentence_alignment_ratio = round(
        audit.translated_sentence_count / max(1, audit.source_sentence_count),
        3,
    )

    if source_text and not translated_text:
        audit.flags.append("empty_translation")
        audit.risk_level = _set_risk(audit.risk_level, "fail")

    title_like = bool(BeautifulSoup(original_html or "", "html.parser").find(re.compile(r"^h[1-6]$")))
    if _likely_untranslated_english(source_text, translated_text, preserved_terms, title_like):
        audit.likely_untranslated = True
        audit.flags.append("likely_untranslated")
        audit.risk_level = _set_risk(audit.risk_level, "fail")

    if error_like_checker and error_like_checker(translated_html):
        audit.error_like_response = True
        audit.flags.append("error_like_response")
        audit.risk_level = _set_risk(audit.risk_level, "fail")

    # 长文本异常过短很可能是漏译/截断；短标题不做长度告警，避免噪音。
    # Chinese characters carry more information than Latin characters. Keep
    # the raw ratio in diagnostics, but do not penalize concise Chinese questions.
    effective_ratio = (translated_len + _cjk_char_count(translated_text)) / max(1, source_len)
    if source_len >= 40 and translated_len > 0 and effective_ratio < 0.25:
        audit.flags.append("suspiciously_short_translation")
        audit.risk_level = _set_risk(audit.risk_level, "warn")

    if (
        source_len >= 80
        and audit.source_sentence_count >= 2
        and (
            audit.sentence_alignment_ratio < 0.35
            or audit.sentence_alignment_ratio > 2.8
        )
    ):
        audit.flags.append("sentence_alignment_suspicious")
        audit.risk_level = _set_risk(audit.risk_level, "warn")

    missing = missing_numeric_facts(_numeric_text(original_html), _numeric_text(translated_html))
    if missing:
        audit.numbers_missing = missing
        audit.flags.append("numbers_missing")
        audit.risk_level = _set_risk(audit.risk_level, "warn")

    missing_markers = _missing_critical_markers(source_text, translated_text)
    if missing_markers:
        audit.critical_markers_missing = missing_markers
        audit.flags.append("critical_markers_missing")
        audit.risk_level = _set_risk(audit.risk_level, "warn")

    # A narrow scope signal, not an automatic rewrite: avoid using != avoid not
    # using. This caught a real-book reversal that word-presence checks missed.
    if (re.search(r'\bavoid\s+(?:using|trading|applying)\b', source_text, re.I)
            and re.search(r'避免[^。！？；]{0,12}(?:不|未|没)(?:使用|交易|采用|运用)', translated_text)):
        audit.flags.append('negation_scope_suspicious')
        audit.risk_level = _set_risk(audit.risk_level, 'warn')

    glossary = glossary or {}
    missing_terms: list[str] = []
    for src, dst in _relevant_glossary_terms(glossary, source_text):
        if dst not in translated_text:
            missing_terms.append(src)
    if missing_terms:
        audit.latin_terms_missing = missing_terms
        audit.flags.append("glossary_terms_missing")
        audit.risk_level = _set_risk(audit.risk_level, "warn")

    if (_tag_counter(original_html) != _tag_counter(translated_html)
            or media_subtrees(original_html) != media_subtrees(translated_html)):
        audit.html_tag_mismatch = True
        audit.flags.append("html_tag_mismatch")
        audit.risk_level = _set_risk(audit.risk_level, "fail")

    return audit
