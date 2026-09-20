"""Build the bounded, editable pre-payment translation preflight payload."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.book_profile_service import profile_book
from app.domain.manifest_service import build_manifest
from app.domain.translation_strategy import (
    TRANSLATION_STRATEGY_LABELS,
    resolve_translation_strategy,
    strategy_recommends_bilingual,
)
from app.engine.glossary_service import build_consistent_glossary
from app.models import ChapterKind
from app.infra.llm_usage_ledger import usage_scope


PREFLIGHT_SCHEMA_VERSION = 1


def _body_texts(manifest: dict[str, Any]) -> list[str]:
    return [
        str(chunk.get("text") or "").strip()
        for chapter in manifest.get("chapters") or []
        if chapter.get("chapter_kind") == ChapterKind.body.value
        for chunk in chapter.get("chunks") or []
        if str(chunk.get("text") or "").strip()
    ]


def _glossary_catalog(glossary_result: Any) -> list[dict[str, Any]]:
    candidates = {
        str(item.term): item
        for item in getattr(glossary_result, "candidates", []) or []
    }
    user_terms = set((getattr(glossary_result, "user_glossary", {}) or {}).keys())
    global_terms = set((getattr(glossary_result, "global_glossary", {}) or {}).keys())
    auto_terms = set((getattr(glossary_result, "auto_glossary", {}) or {}).keys())
    rows: list[dict[str, Any]] = []
    for source, translated in sorted(
        (getattr(glossary_result, "glossary", {}) or {}).items(),
        key=lambda item: str(item[0]).casefold(),
    ):
        candidate = candidates.get(source)
        if source in user_terms:
            origin, status = "user", "confirmed"
        elif source in global_terms:
            origin, status = "global", "confirmed"
        elif source in auto_terms:
            origin, status = "automatic", "generated"
        else:
            origin, status = "automatic", "generated"
        rows.append({
            "source": str(source)[:180],
            "translation": str(translated)[:180],
            "type": (
                sorted(getattr(candidate, "kinds", []) or ["term"])[0]
                if candidate else "term"
            ),
            "aliases": [],
            "first_location": "",
            "source_example": (
                str((getattr(candidate, "contexts", []) or [""])[0])[:320]
                if candidate else ""
            ),
            "origin": origin,
            "confidence": round(float(getattr(candidate, "confidence", 1.0) or 0), 3),
            "status": status,
        })
    return rows[:240]


def _chapters(manifest: dict[str, Any], default_strategy: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chapter in manifest.get("chapters") or []:
        if chapter.get("chapter_kind") != ChapterKind.body.value:
            continue
        file_path = str(chapter.get("file_path") or "")
        # Standalone footnote documents still participate in translation, but
        # inherit the whole-book strategy instead of flooding the confirmation
        # screen with hundreds of one-line overrides.
        if re.search(r"(?:^|page\d+)fn\d+$", Path(file_path).stem, re.IGNORECASE):
            continue
        rows.append({
            "chapter_id": str(chapter.get("chapter_id") or "")[:120],
            "file_path": file_path[:500],
            "chunk_count": len(chapter.get("chunks") or []),
            "strategy": default_strategy,
        })
        if len(rows) >= 500:
            break
    return rows


def build_translation_preflight(
    *, epub_path: str | Path, job_id: str, target_lang: str, translation_model: str,
    requested_strategy: str, user_glossary: dict[str, str] | None = None, billing_engine=None,
) -> dict[str, Any]:
    # Runs in a worker thread before a Job row exists. Carry its allocated id.
    with usage_scope(job_id, "preflight", engine=billing_engine):
        return _build_translation_preflight(epub_path=epub_path, job_id=job_id, target_lang=target_lang,
                                            translation_model=translation_model, requested_strategy=requested_strategy,
                                            user_glossary=user_glossary)


def _build_translation_preflight(
    *,
    epub_path: str | Path,
    job_id: str,
    target_lang: str,
    translation_model: str,
    requested_strategy: str,
    user_glossary: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Analyze a book before payment; failures return a conservative editable payload."""
    try:
        manifest = build_manifest(str(epub_path), job_id)
    except Exception as exc:
        manifest = {
            "job_id": job_id,
            "chapters": [],
            "error": f"preflight manifest failed: {str(exc)[:180]}",
        }
    manifest_error = str(manifest.get("error") or "").strip()
    profile = profile_book(
        epub_path=str(epub_path),
        manifest=manifest,
        model=translation_model,
        target_lang=target_lang,
    )
    resolved_strategy, strategy_source = resolve_translation_strategy(
        requested_strategy,
        profile,
    )
    texts = _body_texts(manifest)
    glossary_result = None
    if texts:
        try:
            glossary_result = build_consistent_glossary(
                texts,
                target_lang=target_lang,
                user_glossary=user_glossary or {},
                min_count=2,
                max_terms=160,
            )
        except Exception:
            glossary_result = None
    glossary = dict(getattr(glossary_result, "glossary", {}) or user_glossary or {})
    characters = [
        dict(item)
        for item in profile.get("characters") or []
        if isinstance(item, dict)
    ][:120]
    for character in characters:
        source = str(character.get("source_name") or "").strip()
        translated = str(character.get("translated_name") or "").strip()
        if source and translated and source not in glossary:
            glossary[source] = translated

    confidence = float(profile.get("confidence") or 0)
    reasons: list[str] = []
    if profile.get("status") != "ok":
        reasons.append("图书探针使用了保守回退结果")
    if confidence < 0.7:
        reasons.append("图书画像置信度低于 70%")
    if "mixed" in {
        str(profile.get("genre") or "").casefold(),
        *(str(item).casefold() for item in profile.get("subgenres") or []),
    }:
        reasons.append("检测到混合文体")
    if manifest_error:
        reasons.append(f"章节解析不完整：{manifest_error[:180]}")

    editable_chapters = _chapters(manifest, resolved_strategy)
    body_chapters = [
        chapter for chapter in manifest.get("chapters") or []
        if chapter.get("chapter_kind") == ChapterKind.body.value
    ]
    return {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "version": 1,
        "status": "ready",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "confirmed": False,
        "requires_explicit_confirmation": True,
        "confirmation_reasons": reasons,
        "profile": profile,
        "requested_strategy": requested_strategy,
        "resolved_strategy": resolved_strategy,
        "strategy_source": strategy_source,
        "strategy_label": TRANSLATION_STRATEGY_LABELS.get(
            resolved_strategy,
            resolved_strategy,
        ),
        "bilingual_recommended": strategy_recommends_bilingual(resolved_strategy),
        "glossary": glossary,
        "glossary_catalog": (
            _glossary_catalog(glossary_result)
            if glossary_result is not None else []
        ),
        "characters": characters,
        "chapters": editable_chapters,
        "chapter_strategy_overrides": {},
        "manifest_summary": {
            "chapter_count": len(editable_chapters),
            "content_file_count": len(body_chapters),
            "chunk_count": sum(
                len(chapter.get("chunks") or [])
                for chapter in body_chapters
            ),
            "error": manifest_error,
        },
    }
