"""D18: Book Profiler, strategy routing, prompt isolation, and persistence."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine

from app.domain.book_profile_service import (
    _normalize_profile,
    build_profiler_input,
    profile_book_async,
)
from app.domain.translation_strategy import (
    build_readonly_chapter_summary,
    relevant_character_context,
    resolve_translation_strategy,
    strategy_allows_literary_polish,
)
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.models import Job, OutputMode
from app.storage_db import Base, PersistentJobStore


def _manifest(text: str) -> dict:
    return {
        "chapters": [{
            "chapter_id": "chapter-1",
            "file_path": "EPUB/chapter-1.xhtml",
            "chapter_kind": "body",
            "chunks": [
                {"text": text, "html": f"<p>{text}</p>"},
                {"text": "The argument continues with a constitutional limitation.", "html": "<p>More.</p>"},
            ],
        }],
    }


def test_profiler_input_is_bounded_and_auditable():
    payload, audit = build_profiler_input(
        epub_path="/path/that/does/not/exist.epub",
        manifest=_manifest("Philosophy and political history. " * 1000),
        book_title="A Serious Book",
        max_chars=6000,
    )
    assert payload["metadata"]["title"] == ["A Serious Book"]
    assert audit["sample_count"] >= 1
    assert audit["sample_chars"] <= 6000
    assert len(audit["samples"][0]["sha256"]) == 64
    assert "text" not in audit["samples"][0]


def test_profiler_disabled_uses_nonblocking_conservative_fallback():
    previous = os.environ.get("EPUB_BOOK_PROFILER_ENABLED")
    os.environ["EPUB_BOOK_PROFILER_ENABLED"] = "0"
    try:
        profile = asyncio.run(profile_book_async(
            epub_path="/missing.epub",
            manifest=_manifest(
                "Political philosophy, revolution, government, constitutional law, and historical sovereignty."
            ),
            book_title="Reflections",
            model="deepseek-v4-flash",
        ))
    finally:
        if previous is None:
            os.environ.pop("EPUB_BOOK_PROFILER_ENABLED", None)
        else:
            os.environ["EPUB_BOOK_PROFILER_ENABLED"] = previous
    assert profile["status"] == "fallback"
    assert profile["recommended_strategy"] == "mirror_fidelity"
    assert profile["sampling"]["sample_count"] >= 1


def test_profile_schema_rejects_freeform_strategy():
    raw = {
        "genre": "fiction",
        "recommended_strategy": "invent_a_new_prompt",
        "confidence": 0.9,
    }
    try:
        _normalize_profile(raw)
    except ValueError as exc:
        assert "unsupported strategy" in str(exc)
    else:
        raise AssertionError("unsupported profiler strategy must be rejected")


def test_user_strategy_overrides_profiler_and_mirror_disables_polish():
    profile = {
        "status": "ok",
        "recommended_strategy": "mirror_fidelity",
    }
    assert resolve_translation_strategy("auto", profile) == ("mirror_fidelity", "profiler")
    assert resolve_translation_strategy("literary_narrative", profile) == (
        "literary_narrative",
        "user",
    )
    assert strategy_allows_literary_polish("mirror_fidelity") is False
    assert strategy_allows_literary_polish("literary_narrative") is True


def test_prompt_and_cache_are_isolated_by_profile_and_strategy():
    previous = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "dummy"
    profile = {
        "status": "ok",
        "genre": "philosophy",
        "tone": ["serious"],
        "recommended_strategy": "mirror_fidelity",
        "characters": [{
            "source_name": "Burke",
            "translated_name": "伯克",
            "aliases": ["Mr. Burke"],
            "role": "author",
            "pronouns": "he",
            "evidence": "named in the sample",
            "confidence": 0.9,
        }],
    }
    try:
        mirror = SemanticsTranslator(
            model="deepseek-v4-flash",
            translation_strategy="mirror_fidelity",
            book_profile=profile,
        )
        neutral = SemanticsTranslator(
            model="deepseek-v4-flash",
            translation_strategy="neutral_faithful",
            book_profile={**profile, "genre": "unknown"},
        )
        prompt = mirror._build_system_prompt()
        assert "镜像级忠实" in prompt
        assert '"genre":"philosophy"' in prompt
        assert mirror._cache_family_key != neutral._cache_family_key
        assert "Burke" in relevant_character_context(profile, "Mr. Burke replied.")
    finally:
        if previous is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = previous


def test_readonly_chapter_summary_is_precomputed_from_source():
    summary = build_readonly_chapter_summary(
        "chapter.xhtml",
        ["<p>First source paragraph.</p>", "<p>Middle source.</p>", "<p>Last source.</p>"],
    )
    assert "章节原文抽样提要" in summary
    assert "First source paragraph" in summary
    assert "Last source" in summary


def test_translation_strategy_persists_in_sql_store():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = PersistentJobStore(engine=engine)
    job = Job(
        id=uuid.uuid4().hex[:12],
        trace_id=uuid.uuid4().hex,
        source_filename="book.epub",
        input_path="/tmp/book.epub",
        output_mode=OutputMode.simplified,
        enable_translation=True,
        translation_strategy="academic_rigorous",
        translation_stats={"book_profile": {"genre": "academic"}},
    )
    store.add(job)
    fetched = store.get(job.id)
    assert fetched is not None
    assert fetched.translation_strategy == "academic_rigorous"
    assert fetched.translation_stats["book_profile"]["genre"] == "academic"


if __name__ == "__main__":
    tests = [
        test_profiler_input_is_bounded_and_auditable,
        test_profiler_disabled_uses_nonblocking_conservative_fallback,
        test_profile_schema_rejects_freeform_strategy,
        test_user_strategy_overrides_profiler_and_mirror_disables_polish,
        test_prompt_and_cache_are_isolated_by_profile_and_strategy,
        test_readonly_chapter_summary_is_precomputed_from_source,
        test_translation_strategy_persists_in_sql_store,
    ]
    for test_fn in tests:
        test_fn()
        print(f"  ✅ {test_fn.__name__}")
    print(f"\n📊 {len(tests)} passed, 0 failed")
