"""Validated QA-only alias evidence, separate from generation settings."""

from collections import Counter, defaultdict

from app.domain.translation_terminology_policy import term_key


def normalize_terminology_review(rows, glossary, characters=(), *, strict=False):
    """Fail closed on stale, duplicate or unrelated mappings; never infer aliases.

    Public confirmation rejects invalid edits. Reading an older snapshot drops
    invalid evidence instead, so it cannot relax the current mapping contract.
    Entity IDs are normalized full source names (or the term itself), matching
    the consistency audit's identity convention.
    """
    def invalid():
        if strict:
            raise ValueError("术语复核记录无效：请核对当前译名、唯一实体和已确认别名")

    if not isinstance(rows, list) or len(rows) > 240:
        invalid()
        return []
    mappings = defaultdict(list)
    for source, target in glossary.items():
        mappings[term_key(source)].append((source, target))
    owners = defaultdict(set)
    for character in characters or []:
        if not isinstance(character, dict):
            continue
        name = character.get("source_name")
        if not isinstance(name, str) or not name.strip():
            continue
        aliases = character.get("aliases")
        for source in [name, *(aliases if isinstance(aliases, list) else [])]:
            if isinstance(source, str) and source.strip():
                owners[term_key(source)].add(term_key(name))
    counts = Counter(term_key(row["source"]) for row in rows if isinstance(row, dict)
                     and isinstance(row.get("source"), str))
    result = []
    for row in rows:
        if not isinstance(row, dict):
            invalid()
            continue
        source, target = row.get("source"), row.get("translation")
        identity = row.get("entity_id")
        aliases = row.get("target_aliases", [])
        if (not isinstance(source, str) or not 0 < len(source.strip()) <= 180
                or not isinstance(target, str) or not 0 < len(target.strip()) <= 180
                or row.get("status") != "confirmed"
                or (identity is not None and (not isinstance(identity, str) or not identity.strip()
                                             or len(identity) > 180))
                or not isinstance(aliases, list) or len(aliases) > 12):
            invalid()
            continue
        key = term_key(source)
        identity = term_key(identity or source)
        matches = mappings.get(key, [])
        if (counts[key] != 1 or len(matches) != 1 or matches[0][1] != target
                or identity not in (owners.get(key) or {key})):
            invalid()
            continue
        accepted = []
        valid = True
        for alias in aliases:
            if (not isinstance(alias, dict) or alias.get("status") != "confirmed"
                    or not isinstance(alias.get("translation"), str)
                    or not 0 < len(alias["translation"].strip()) <= 180
                    or ("entity_id" in alias and (not isinstance(alias["entity_id"], str)
                        or term_key(alias["entity_id"]) != identity))):
                valid = False
                break
            translated = alias["translation"].strip()
            if translated != target and translated not in [a["translation"] for a in accepted]:
                accepted.append({"translation": translated, "status": "confirmed", "entity_id": identity})
        if not valid:
            invalid()
            continue
        result.append({"source": matches[0][0], "translation": target, "entity_id": identity,
                       "status": "confirmed", "target_aliases": accepted})
    return sorted(result, key=lambda row: term_key(row["source"]))


def preflight_terminology_review(preflight):
    """Read legacy evidence without rewriting the saved generation snapshot."""
    if "terminology_review" in preflight:
        return preflight["terminology_review"]
    catalog = preflight.get("glossary_catalog") or []
    return [row for row in catalog if isinstance(row, dict)
            and ("entity_id" in row or "target_aliases" in row)]


def confirmed_terminology_review(existing, payload, glossary, characters):
    """Accept explicit review rows, or metadata on catalog/character edits.

    The dedicated field is a replacement (including [] to revoke all evidence).
    Catalog/character edits patch matching sources; omitted evidence survives
    only while its mapping and identity are still valid.
    """
    if "terminology_review" in payload:
        if "glossary_catalog" in payload or any(isinstance(row, dict)
                and ("entity_id" in row or "target_aliases" in row)
                for row in payload.get("characters", []) or []):
            raise ValueError("请只提交一种术语复核记录格式")
        return normalize_terminology_review(payload["terminology_review"], glossary, characters, strict=True)
    incoming = []
    if "glossary_catalog" in payload:
        catalog = payload["glossary_catalog"]
        if (not isinstance(catalog, list) or len(catalog) > 240
                or any(not isinstance(row, dict) for row in catalog)):
            raise ValueError("glossary_catalog 必须是最多 240 项的数组")
        incoming.extend(row for row in catalog if isinstance(row, dict)
                        and ("entity_id" in row or "target_aliases" in row))
    for character in payload.get("characters", []) or []:
        if isinstance(character, dict) and ("entity_id" in character or "target_aliases" in character):
            incoming.append({"source": character.get("source_name"),
                             "translation": character.get("translated_name"),
                             "status": character.get("status", "confirmed"),
                             **{key: character[key] for key in ("entity_id", "target_aliases") if key in character}})
    incoming = normalize_terminology_review(incoming, glossary, characters, strict=True)
    inherited = normalize_terminology_review(preflight_terminology_review(existing), glossary, characters)
    replacements = {term_key(row["source"]) for row in incoming}
    return sorted([row for row in inherited if term_key(row["source"]) not in replacements] + incoming,
                  key=lambda row: term_key(row["source"]))


def apply_terminology_review(catalog, review, characters=()):
    mappings = {row["source"]: row["translation"] for row in catalog}
    validated = normalize_terminology_review(review or [], mappings, characters)
    metadata = {row["source"]: row for row in validated}
    return [dict(row, **metadata[row["source"]], origin="user")
            if row["source"] in metadata else dict(row) for row in catalog]


def generation_preflight(preflight):
    """Keep legacy resume hashes byte-for-byte; exclude only the new QA field.

    Do not strip legacy catalog/profile fields, timestamps or versions: they
    already participate in saved keys. Meaning-changing settings still bind
    checkpoints. This projection is not a migration of historical snapshots.
    """
    if not isinstance(preflight, dict):
        return preflight
    return {key: value for key, value in preflight.items() if key != "terminology_review"}
