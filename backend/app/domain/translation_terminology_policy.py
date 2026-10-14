"""Read-only terminology evidence; generated suggestions are not contracts."""

from __future__ import annotations

import unicodedata

from app.domain.translation_terms import relevant_terms, target_term_present, ambiguous_short_name


def term_key(term: str) -> str:
    return " ".join(term.split()).casefold()


def terminology_evidence(source: str, target: str, glossary: dict[str, str],
                         catalog: list[dict] | None = None) -> list[dict]:
    # Only metadata for the actual mapping can relax its legacy contract.
    # An old/stale catalog must not downgrade a new user override.
    metadata = {}
    for row in catalog or []:
        if isinstance(row, dict) and isinstance(row.get("source"), str):
            metadata.setdefault(term_key(row["source"]), []).append(row)
    evidence = []
    for source_term, expected in relevant_terms(glossary, source).items():
        rows = [r for r in metadata.get(term_key(source_term), [])
                if r.get("translation") == expected]
        row = rows[0] if len(rows) == 1 else {}
        origin, status = row.get("origin", "unknown"), row.get("status", "unknown")
        generated = origin in {"auto", "book_profiler"} and status == "generated"
        targets = [expected]
        # Do not accept a bare/generated alias, fuzzy spelling, brand category,
        # or an alias belonging to another entity as proof of translation.
        for alias in row.get("target_aliases", []) if isinstance(row.get("target_aliases"), list) else []:
            if (isinstance(alias, dict) and alias.get("status") == "confirmed"
                    and isinstance(alias.get("translation"), str) and alias["translation"].strip()
                    and (not alias.get("entity_id") or alias["entity_id"] == row.get("entity_id"))):
                targets.append(alias["translation"].strip())
        short_cjk = ambiguous_short_name(source_term, expected) or (generated
            and len(expected.strip()) == 1 and "CJK" in unicodedata.name(expected.strip(), ""))
        matched = [value for value in targets if target_term_present(
            value, target, source_term=source_term, require_isolated_short=generated)]
        category = ("matched" if matched else
                    "glossary_mapping_ambiguous" if generated and short_cjk else
                    "glossary_candidate_difference" if generated else "glossary_terms_missing")
        evidence.append({
            "source_term": source_term, "expected_translation": expected,
            "origin": origin, "status": status, "category": category,
            "entity_id": str(row.get("entity_id") or term_key(source_term)),
            "matched_translations": matched,
        })
    return evidence
