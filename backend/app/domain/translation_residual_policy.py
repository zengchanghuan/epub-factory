"""Shared untranslated-text policy, including exact user-approved preservation."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import urlsplit
from app.domain.translation_titles import COMMON_ZH_TITLES

_SHORT_RESPONSES = {'yes', 'no', 'exactly', 'yes, exactly', 'absolutely', 'certainly',
                    'correct', 'of course', 'not exactly', 'no, not yet', "that's right"}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip().casefold()


def confirmed_preserved_terms(glossary: dict | None) -> list[str]:
    # Only an explicit source -> same source entry requests preservation.
    return [str(source).strip() for source, target in (glossary or {}).items()
            if str(source).strip() and normalize_text(str(source)) == normalize_text(str(target))]


def is_preserved_reference(text: str) -> bool:
    """Only a complete URL/DOI is exempt; surrounding prose is not."""
    value = unicodedata.normalize("NFKC", text or "").strip()
    if re.fullmatch(r"(?:doi:\s*)?10\.\d{4,9}/[^\s<>\"\u201c\u201d]+", value, re.I):
        return True
    if not re.fullmatch(r"(?:https?://|www\.)[^\s<>\"\u201c\u201d]+", value, re.I):
        return False
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        return bool(parsed.hostname and not parsed.username and not parsed.password
                    and parsed.scheme.lower() in {"http", "https"})
    except ValueError:
        return False


def is_preserved_reference_node(node) -> bool:
    """A bare domain is a reference only when its enclosing link proves it."""
    text = str(node).strip()
    if is_preserved_reference(text):
        return True
    anchor = node.find_parent('a', href=True)
    if anchor is None or anchor.get_text().strip() != text:
        return False
    try:
        destination = urlsplit(anchor.get('href', ''))
        return bool(destination.scheme.lower() in {'https', 'http'}
                    and destination.hostname and not destination.username and not destination.password
                    and '.' in text and destination.hostname.casefold() == text.casefold())
    except ValueError:
        return False


def without_reference_text_nodes(html: str) -> str:
    """Audit-only copy: an intact standalone URL must not inflate English ratios.

    Do not strip arbitrary URL substrings (which might consume adjacent prose),
    attributes, or link labels such as "Read the full article". Delivery markup
    is untouched; only complete URL/DOI text nodes are excluded from language QA.
    """
    from bs4 import BeautifulSoup, Comment
    soup = BeautifulSoup(html or '', 'html.parser')
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, Comment) and is_preserved_reference_node(node):
            node.replace_with('')
    return str(soup)


def residual_category(text: str, *, source_text: str | None = None,
                      title_like: bool = False, preserved_terms: Iterable[str] = ()) -> str:
    preserved_terms = tuple(preserved_terms)
    normalized = normalize_text(text)
    if (not normalized or is_preserved_reference(text)
            or normalized in {normalize_text(term) for term in preserved_terms}):
        return ""
    script_text = text
    for term in sorted(preserved_terms, key=lambda value: -len(value)):
        if term:
            script_text = re.sub(re.escape(term), '', script_text, flags=re.I)
    kana = len(re.findall(r'[\u3041-\u3096\u30a1-\u30fa]', script_text))
    if kana and source_text and normalized == normalize_text(source_text):
        return 'unchanged_japanese_source'
    if kana >= 3:
        return 'japanese_script_residual'
    latin = sum("LATIN" in unicodedata.name(ch, "") for ch in text)
    words = [word for word in re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", text)
             if any("LATIN" in unicodedata.name(ch, "") for ch in word)]
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    response = normalized.rstrip('.!?。！？').strip()
    # A bounded set of ordinary answers, not arbitrary names/acronyms. "NO"
    # can be a formula; "No." is a sentence. Explicit preservation stays first.
    acronym = len(response) <= 3 and text.rstrip('.!?').isupper()
    if cjk == 0 and response in _SHORT_RESPONSES and not acronym:
        return 'short_english_response'
    if title_like and cjk == 0 and (normalized in COMMON_ZH_TITLES
                                    or (len(words) >= 2 and latin >= 8)):
        return "short_english_title"
    if source_text and normalized == normalize_text(source_text) and len(words) >= 2 and latin >= 12 and cjk == 0:
        return "unchanged_source"
    if len(words) >= 10 and latin >= 80 and cjk == 0:
        return "long_english_no_cjk"
    if len(words) >= 6 and cjk < max(6, int(latin * 0.15)):
        return "likely_untranslated"
    if cjk > 0 and len(words) >= 12 and latin > max(120, cjk * 2.5):
        return "mixed_latin_dominant"
    return ""
