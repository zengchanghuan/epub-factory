"""Deterministic whole-book terminology and character consistency signals."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.domain.translation_terms import visible_term_text, relevant_terms, term_spans


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
    review_chunks: set[tuple[str, int]] = set()
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

    # The merged glossary already carries user/global precedence. A profile
    # may fill gaps, but must never silently override a confirmed translation.
    canonical = dict(glossary or {})
    for source, target in character_terms.items():
        # Case/whitespace-equivalent Latin keys match the same source span.
        # Adding the profile spelling would otherwise win the lexical tie.
        if not any(isinstance(term, str) and term_spans(term, source) == [(0, len(source))]
                   for term in canonical):
            canonical[source] = target
    for chapter_id, results in chapter_results.items():
        for index, result in enumerate(results):
            source = visible_term_text(getattr(result, "original_html", ""))
            translated = visible_term_text(getattr(result, "translated_html", ""))
            if not source or not translated or getattr(result, "error", None):
                continue
            for source_term, target_term in relevant_terms(canonical, source).items():
                checks += 1
                if term_spans(target_term, translated):
                    continue
                term_counts[source_term] += 1
                chapter_counts[chapter_id] += 1
                review_chunks.add((chapter_id, index))
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
        "consistency_status": "review" if term_counts else ("passed" if checks else "not_checked"),
        "consistency_scope": "configured_glossary_and_profile_only",
        "consistency_review_chunks": len(review_chunks),
        "consistency_checks": checks,
        "consistency_violations": sum(term_counts.values()),
        "consistency_terms_affected": len(term_counts),
        "consistency_chapters_affected": len(chapter_counts),
        "consistency_violation_terms": dict(term_counts.most_common(30)),
        "consistency_violation_chapters": dict(chapter_counts.most_common(30)),
        "consistency_examples": violations,
        "delivery_gate": False,
    }
