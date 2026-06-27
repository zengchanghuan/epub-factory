"""
Service-wide glossary orchestration.

This layer makes terminology consistency a backend concern instead of a
per-runner detail:
- load curated global glossary entries;
- extract and de-noise book-specific candidates;
- optionally translate candidates through the existing LLM glossary path;
- merge with deterministic priority: auto < global < user.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .glossary_extractor import (
    GlossaryCandidate,
    extract_candidates,
    merge_glossaries,
    translate_glossary,
)

logger = logging.getLogger("epub_factory.glossary_service")


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "glossary"

_GENERIC_TERMS = {
    "All", "April", "August", "English", "First", "God", "I’d", "I’m", "I’ve",
    "March", "Most", "New", "Not", "Office", "One", "People", "September",
    "That", "There", "Western", "We’re",
}

_NOISY_ACRONYMS = {
    "AFTER", "ALL", "AND", "BACK", "DAY", "DVD", "END", "FOR", "GOD", "III",
    "IRAQ", "LATE", "MARCH", "NEW", "NOT", "NOW", "ONE", "SEN", "THE", "THIS",
    "WAR", "WAS", "WHAT", "WITH",
}

_ALLOWED_ACRONYMS = {
    "AEI", "CIA", "CPA", "DOD", "DPG", "FBI", "IMF", "INC", "NATO", "NDI",
    "NGO", "NSC", "ORHA", "PNAC", "SCIRI", "UN", "USAID", "WMD",
}


@dataclass
class GlossaryBuildResult:
    glossary: dict[str, str]
    global_glossary: dict[str, str] = field(default_factory=dict)
    auto_glossary: dict[str, str] = field(default_factory=dict)
    user_glossary: dict[str, str] = field(default_factory=dict)
    candidates: list[GlossaryCandidate] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def load_global_glossary(target_lang: str = "zh-CN") -> dict[str, str]:
    """Load curated, service-wide glossary entries for the target language."""
    path = _DATA_DIR / f"global_{target_lang}.json"
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("global glossary load failed: %s", exc)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(k).strip(): str(v).strip()
        for k, v in parsed.items()
        if str(k).strip() and str(v).strip()
    }


def _normalize_candidate_term(term: str) -> str:
    term = re.sub(r"\s+", " ", term or "").strip()
    term = re.sub(r"(?:'|’)s$", "", term)
    return term.strip(".,;:!?\"'()[]{}")


def filter_candidates(
    candidates: Iterable[GlossaryCandidate],
    *,
    max_terms: int = 160,
) -> list[GlossaryCandidate]:
    """Remove obvious false positives and normalize possessive variants."""
    out: list[GlossaryCandidate] = []
    seen: set[str] = set()
    for c in candidates:
        term = _normalize_candidate_term(c.term)
        if not term or term in seen:
            continue
        if term in _GENERIC_TERMS:
            continue
        if re.fullmatch(r"[A-Z]{2,8}", term):
            if term in _NOISY_ACRONYMS or term not in _ALLOWED_ACRONYMS:
                continue
        out.append(GlossaryCandidate(
            term=term,
            count=c.count,
            confidence=c.confidence,
            kinds=set(c.kinds),
        ))
        seen.add(term)
        if len(out) >= max_terms:
            break
    return out


async def build_consistent_glossary_async(
    texts: Iterable[str],
    *,
    target_lang: str = "zh-CN",
    user_glossary: dict[str, str] | None = None,
    min_count: int = 2,
    max_terms: int = 160,
) -> GlossaryBuildResult:
    """
    Build the glossary used by a translation job.

    Priority is deliberate:
    - auto glossary fills book-specific gaps;
    - curated global entries override unstable auto translations;
    - user entries override everything.
    """
    user_glossary = user_glossary or {}
    raw_candidates, extraction_stats = extract_candidates(
        texts,
        min_count=min_count,
        max_terms=max(max_terms * 3, max_terms),
    )
    candidates = filter_candidates(raw_candidates, max_terms=max_terms)
    global_glossary = load_global_glossary(target_lang)
    auto_glossary: dict[str, str] = {}

    try:
        auto_glossary = await translate_glossary(
            candidates,
            target_lang=target_lang,
            max_terms_per_call=80,
        )
    except Exception as exc:
        logger.warning("auto glossary translation skipped: %s", exc)
        auto_glossary = {}

    merged = merge_glossaries(global_glossary, auto_glossary)
    merged = merge_glossaries(user_glossary, merged)
    return GlossaryBuildResult(
        glossary=merged,
        global_glossary=global_glossary,
        auto_glossary=auto_glossary,
        user_glossary=user_glossary,
        candidates=candidates,
        stats={
            "total_chars_scanned": extraction_stats.total_chars_scanned,
            "raw_candidates": extraction_stats.raw_candidates,
            "after_filter": extraction_stats.after_filter,
            "candidate_count": len(candidates),
            "auto_glossary_count": len(auto_glossary),
            "global_glossary_count": len(global_glossary),
            "user_glossary_count": len(user_glossary),
            "merged_glossary_count": len(merged),
        },
    )


def build_consistent_glossary(
    texts: Iterable[str],
    *,
    target_lang: str = "zh-CN",
    user_glossary: dict[str, str] | None = None,
    min_count: int = 2,
    max_terms: int = 160,
) -> GlossaryBuildResult:
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
    return loop.run_until_complete(build_consistent_glossary_async(
        texts,
        target_lang=target_lang,
        user_glossary=user_glossary,
        min_count=min_count,
        max_terms=max_terms,
    ))
