"""Shared untranslated-text policy, including exact user-approved preservation."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip().casefold()


def confirmed_preserved_terms(glossary: dict | None) -> list[str]:
    # Only an explicit source -> same source entry requests preservation.
    return [str(source).strip() for source, target in (glossary or {}).items()
            if str(source).strip() and normalize_text(str(source)) == normalize_text(str(target))]


def residual_category(text: str, *, source_text: str | None = None,
                      title_like: bool = False, preserved_terms: Iterable[str] = ()) -> str:
    normalized = normalize_text(text)
    if not normalized or normalized in {normalize_text(term) for term in preserved_terms}:
        return ""
    latin = sum("LATIN" in unicodedata.name(ch, "") for ch in text)
    words = [word for word in re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", text)
             if any("LATIN" in unicodedata.name(ch, "") for ch in word)]
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    if title_like and len(words) >= 2 and latin >= 12 and cjk == 0:
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
