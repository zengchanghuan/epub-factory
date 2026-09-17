"""Deterministic whole-book terminology and character consistency signals."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from bs4 import BeautifulSoup


def _visible(html: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        BeautifulSoup(str(html or ""), "html.parser").get_text(" ", strip=True),
    ).strip()


def _contains(text: str, term: str) -> bool:
    if not text or not term:
        return False
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9\s'’.\-]*", term):
        return bool(re.search(r"\b" + re.escape(term) + r"\b", text, re.I))
    return term in text


def audit_book_consistency(
    *,
    chapter_results: dict[str, list[Any]],
    glossary: dict[str, str],
    characters: list[dict[str, Any]] | None = None,
    example_limit: int = 30,
) -> dict[str, Any]:
    """Aggregate missing canonical translations across chapters without blocking delivery."""
    checks = 0
    violations: list[dict[str, str]] = []
    term_counts: Counter[str] = Counter()
    chapter_counts: Counter[str] = Counter()
    character_terms: dict[str, str] = {}
    for character in characters or []:
        if not isinstance(character, dict):
            continue
        translated = str(character.get("translated_name") or "").strip()
        if not translated:
            continue
        for source in [
            character.get("source_name"),
            *((character.get("aliases") or []) if isinstance(character.get("aliases"), list) else []),
        ]:
            source_text = str(source or "").strip()
            if source_text:
                character_terms[source_text] = translated

    canonical = dict(glossary or {})
    canonical.update(character_terms)
    for chapter_id, results in chapter_results.items():
        for result in results:
            source = _visible(getattr(result, "original_html", ""))
            translated = _visible(getattr(result, "translated_html", ""))
            if not source or not translated or getattr(result, "error", None):
                continue
            for source_term, target_term in canonical.items():
                if not _contains(source, source_term):
                    continue
                checks += 1
                if _contains(translated, target_term):
                    continue
                term_counts[source_term] += 1
                chapter_counts[chapter_id] += 1
                if len(violations) < example_limit:
                    violations.append({
                        "chapter_id": chapter_id,
                        "chunk_id": str(getattr(result, "chunk_id", "") or ""),
                        "source_term": source_term,
                        "expected_translation": target_term,
                        "source_text": source[:180],
                        "translated_text": translated[:180],
                    })

    return {
        "consistency_checks": checks,
        "consistency_violations": sum(term_counts.values()),
        "consistency_terms_affected": len(term_counts),
        "consistency_chapters_affected": len(chapter_counts),
        "consistency_violation_terms": dict(term_counts.most_common(30)),
        "consistency_violation_chapters": dict(chapter_counts.most_common(30)),
        "consistency_examples": violations,
        "delivery_gate": False,
    }
