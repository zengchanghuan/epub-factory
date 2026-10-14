"""Read-only terminology evidence; generated suggestions are not contracts."""

from __future__ import annotations

import unicodedata
import re
from collections import defaultdict

from app.domain.translation_terms import (relevant_term_occurrences, target_term_spans,
                                         ambiguous_short_name, parenthetical_spans)


def term_key(term: str) -> str:
    return " ".join(term.split()).casefold()


def _non_overlapping(spans):
    selected = []
    for a, b in sorted(set(spans), key=lambda span: (-(span[1] - span[0]), span[0])):
        if not any(a < end and b > start for start, end in selected):
            selected.append((a, b))
    return sorted(selected)


def _parenthetical_variants(text, accepted):
    # Only an explicit dotted CJK name immediately beside a parenthesized
    # dotted name is evidence. Never infer a translation from a fuzzy spelling.
    name = r"[\u3400-\u9fff]+(?:[·・][\u3400-\u9fff]+)+"
    for a, b in parenthetical_spans(text):
        left = re.search(name + r"$", text[:a].rstrip())
        right = re.match(name, text[a + 1:b - 1].lstrip())
        if not left or not right:
            continue
        # The left match can include unsegmented Chinese prose. Compare only
        # the known suffix, while requiring the other side to be name-shaped.
        left_known = any(left.group().endswith(value) for value in accepted)
        right_known = right.group() in accepted
        if (left_known != right_known) and not left.group().endswith(right.group()):
            yield (a, b)


def terminology_evidence(source: str, target: str, glossary: dict[str, str],
                         catalog: list[dict] | None = None) -> list[dict]:
    # Only metadata for the actual mapping can relax its legacy contract.
    # An old/stale catalog must not downgrade a new user override.
    metadata = {}
    for row in catalog or []:
        if isinstance(row, dict) and isinstance(row.get("source"), str):
            metadata.setdefault(term_key(row["source"]), []).append(row)
    occurrences = relevant_term_occurrences(glossary, source)
    source_spans = defaultdict(list)
    for a, b, term, _ in occurrences:
        source_spans[term].append((a, b))
    evidence = []
    for source_term, expected in {src: dst for _, _, src, dst in occurrences}.items():
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
        matches = {value: target_term_spans(value, target, source_term=source_term,
                    require_isolated_short=generated) for value in dict.fromkeys(targets)}
        matched = [value for value, spans in matches.items() if spans]
        category = ("matched" if matched else
                    "glossary_mapping_ambiguous" if generated and short_cjk else
                    "glossary_candidate_difference" if generated else "glossary_terms_missing")
        evidence.append({
            "source_term": source_term, "expected_translation": expected,
            "origin": origin, "status": status, "category": category,
            "entity_id": str(row.get("entity_id") or term_key(source_term)),
            "matched_translations": matched,
            "source_spans": source_spans[source_term],
            "target_spans": _non_overlapping(span for spans in matches.values() for span in spans),
            "review_reasons": [],
            "parenthetical_variant_spans": list(_parenthetical_variants(target, targets)),
        })

    # Share evidence only for explicitly linked source identities. A target
    # substring cannot prove two different people were both translated.
    entities = defaultdict(lambda: {"source": [], "target": []})
    for row in evidence:
        group = entities[row["entity_id"]]
        group["source"].extend(row["source_spans"])
        group["target"].extend(row["target_spans"])
    for group in entities.values():
        group["source"] = _non_overlapping(group["source"])
        group["target"] = _non_overlapping(group["target"])
    for identity, group in entities.items():
        # A full target name owns its interior surname. The surname may still
        # match independently elsewhere; equal or crossing spans stay unowned.
        group["exclusive_target"] = [(a, b) for a, b in group["target"] if not any(
            a < d and b > c and not (a <= c and d <= b and (a, b) != (c, d))
            for other, spans in entities.items() if other != identity for c, d in spans["target"])]
    for row in evidence:
        identity = row["entity_id"]
        group = entities[identity]
        reasons = row["review_reasons"]
        if (len(group["exclusive_target"]) < len(group["target"])
                and len(group["exclusive_target"]) < len(group["source"])):
            reasons.append("shared_target_span")
        if group["exclusive_target"] and len(group["exclusive_target"]) < len(group["source"]):
            # Pronouns and ellipsis can be legitimate. This is a review signal,
            # never a missing-content verdict or an instruction to rewrite.
            reasons.append("source_occurrence_gap")
        if row["parenthetical_variant_spans"]:
            reasons.append("parenthetical_name_variants")
        if reasons:
            row["category"] = "glossary_occurrence_unresolved"
        row["source_occurrences"] = len(group["source"])
        row["target_occurrences"] = len(group["exclusive_target"])
        row["unambiguous_target_spans"] = group["exclusive_target"]
    return evidence
