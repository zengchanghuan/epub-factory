"""Deterministic whole-book terminology and character consistency signals."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from app.domain.translation_terms import visible_term_text, term_spans, relevant_terms
from app.domain.translation_terminology_policy import terminology_evidence, term_key


def audit_book_consistency(
    *,
    chapter_results: dict[str, list[Any]],
    glossary: dict[str, str],
    characters: list[dict[str, Any]] | None = None,
    example_limit: int = 30,
    glossary_catalog: list[dict] | None = None,
) -> dict[str, Any]:
    """Aggregate missing canonical translations across chapters without blocking delivery."""
    checks = 0
    violations: list[dict[str, Any]] = []
    term_counts: Counter[str] = Counter()
    chapter_counts: Counter[str] = Counter()
    review_chunks: set[tuple[str, int]] = set()
    character_terms = defaultdict(list)
    catalog = list(glossary_catalog or [])
    categories: Counter[str] = Counter()
    entity_usage = defaultdict(lambda: {"matched": 0, "different": 0, "source_terms": set()})
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
                character_terms[term_key(source_text)].append({
                    "source": source_text, "translation": translated,
                    "origin": "book_profiler", "status": "generated",
                    "entity_id": term_key(str(character.get("source_name") or source_text)),
                })

    # The merged glossary already carries user/global precedence. A profile
    # may fill gaps, but must never silently override a confirmed translation.
    canonical = dict(glossary or {})
    ambiguous_aliases = []
    for candidates in character_terms.values():
        row = candidates[0]
        source = row["source"]
        # Two people sharing a surname is not evidence that they are one entity.
        if len({(r["entity_id"], r["translation"]) for r in candidates}) > 1:
            ambiguous_aliases.append(source)
            continue
        if not any(isinstance(term, str) and term_spans(term, source) == [(0, len(source))]
                   for term in canonical):
            canonical[source] = row["translation"]
            catalog.append(row)
    unresolved = {alias: alias for alias in ambiguous_aliases if not any(
        term_key(alias) == term_key(term) for term in canonical)}
    for chapter_id, results in chapter_results.items():
        for index, result in enumerate(results):
            source = visible_term_text(getattr(result, "original_html", ""))
            translated = visible_term_text(getattr(result, "translated_html", ""))
            if not source or not translated or getattr(result, "error", None):
                continue
            findings = terminology_evidence(source, translated, canonical, catalog)
            for alias in relevant_terms({**canonical, **unresolved}, source) if unresolved else ():
                if alias in unresolved:
                    findings.append({"source_term": alias, "expected_translation": "",
                                     "origin": "book_profiler", "status": "generated",
                                     "category": "glossary_mapping_ambiguous",
                                     "entity_id": "ambiguous:" + term_key(alias),
                                     "matched_translations": []})
            for finding in findings:
                source_term = finding["source_term"]
                checks += 1
                usage = entity_usage[finding["entity_id"]]
                usage["source_terms"].add(source_term)
                if finding["category"] == "matched":
                    usage["matched"] += 1
                    continue
                if finding.get("target_occurrences", 0):
                    usage["matched"] += 1
                usage["different"] += 1
                categories[finding["category"]] += 1
                term_counts[source_term] += 1
                chapter_counts[chapter_id] += 1
                review_chunks.add((chapter_id, index))
                if len(violations) < example_limit:
                    violations.append({
                        **finding,
                        "chapter_id": chapter_id,
                        "chunk_id": str(getattr(result, "chunk_id", "") or ""),
                        "source_text": source[:180],
                        "translated_text": translated[:180],
                    })

    # Mixed evidence is a book-level review signal, not a guessed alias or an
    # instruction to rewrite the majority spelling. Shared surnames are never
    # linked automatically; explicit entity IDs may connect reviewed mappings.
    mixed = [{"entity_id": identity, "source_terms": sorted(usage["source_terms"]),
              "matched_occurrences": usage["matched"], "different_occurrences": usage["different"]}
             for identity, usage in entity_usage.items() if usage["matched"] and usage["different"]]

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
        "consistency_categories": dict(categories),
        "consistency_ambiguous_source_aliases": sorted(ambiguous_aliases),
        "consistency_mixed_entities": mixed[:example_limit],
        "consistency_mixed_entity_count": len(mixed),
        "delivery_gate": False,
    }
