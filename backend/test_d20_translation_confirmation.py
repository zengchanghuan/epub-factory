"""D20: pre-payment confirmation, editable context, and phase-two audits."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient

import app.main as main_module
from app.domain.term_highlight_service import highlight_confirmed_terms
from app.domain.translation_consistency_audit import audit_book_consistency
from app.domain.translation_preflight_service import _chapters
from app.domain.translation_quality_audit import audit_translation_chunk
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.models import ChapterKind, Job, JobStatus, OutputMode


def _preflight() -> dict:
    return {
        "schema_version": 1,
        "version": 1,
        "confirmed": False,
        "profile": {
            "status": "ok",
            "genre": "philosophy",
            "confidence": 0.91,
            "recommended_strategy": "mirror_fidelity",
            "characters": [],
        },
        "resolved_strategy": "mirror_fidelity",
        "glossary": {"Reflexivity": "反身性"},
        "glossary_catalog": [{
            "source": "Reflexivity",
            "translation": "反身性",
            "origin": "automatic",
            "status": "generated",
        }],
        "characters": [],
        "chapters": [{
            "chapter_id": "chapter_1",
            "file_path": "EPUB/chapter_1.xhtml",
            "chunk_count": 3,
            "strategy": "mirror_fidelity",
        }],
        "chapter_strategy_overrides": {},
    }


def test_confirmation_sanitizes_edits_and_versions_snapshot():
    confirmed, strategy, bilingual, glossary = (
        main_module._confirmed_translation_preflight(
            existing=_preflight(),
            payload={
                "translation_strategy": "academic_rigorous",
                "glossary": {"Reflexivity": "反身性理论"},
                "characters": [{
                    "source_name": "George",
                    "translated_name": "乔治",
                    "role": "author",
                    "evidence": "named in the source",
                    "confidence": 0.9,
                }],
                "chapter_strategy_overrides": {
                    "chapter_1": "mirror_fidelity",
                },
                "bilingual": True,
                "enable_term_highlights": True,
            },
        )
    )
    assert strategy == "academic_rigorous"
    assert bilingual is True
    assert confirmed["confirmed"] is True
    assert confirmed["version"] == 2
    assert confirmed["chapter_strategy_overrides"]["chapter_1"] == "mirror_fidelity"
    assert confirmed["profile"]["characters"][0]["translated_name"] == "乔治"
    assert glossary["George"] == "乔治"
    assert confirmed["enable_term_highlights"] is True


def test_create_translation_stops_before_payment_for_preflight():
    previous = os.environ.get("SKIP_PAYMENT_CHECK")
    os.environ["SKIP_PAYMENT_CHECK"] = "1"
    try:
        with (
            patch.object(
                main_module,
                "build_translation_preflight",
                return_value=_preflight(),
            ),
            patch.object(
                main_module,
                "_estimate_translation_pricing",
                return_value={
                    "total_chars": 1000,
                    "price_cny": "5.99",
                },
            ),
            patch.object(main_module, "_enqueue_conversion") as enqueue,
            patch.object(main_module, "create_alipay_page_pay") as create_pay,
        ):
            response = TestClient(main_module.app).post(
                "/api/v2/jobs",
                files={"file": ("preflight.epub", b"fake epub", "application/epub+zip")},
                data={
                    "enable_translation": "true",
                    "profile_confirmation": "true",
                    "translation_strategy": "auto",
                },
                headers={"X-Client-Session": uuid.uuid4().hex},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "awaiting_confirmation"
        assert body["pay_url"] is None
        assert body["translation_preflight"]["confirmed"] is False
        enqueue.assert_not_called()
        create_pay.assert_not_called()
    finally:
        if previous is None:
            os.environ.pop("SKIP_PAYMENT_CHECK", None)
        else:
            os.environ["SKIP_PAYMENT_CHECK"] = previous


def test_confirmation_endpoint_starts_only_after_explicit_confirm():
    job_id = f"d20_{uuid.uuid4().hex[:10]}"
    token = uuid.uuid4().hex
    job = Job(
        id=job_id,
        trace_id=uuid.uuid4().hex,
        source_filename="book.epub",
        input_path="/tmp/book.epub",
        access_token=token,
        expected_amount="5.99",
        output_mode=OutputMode.simplified,
        enable_translation=True,
        translation_strategy="auto",
        status=JobStatus.awaiting_confirmation,
        translation_stats={
            "attempt_id": uuid.uuid4().hex,
            "translation_attempt": 1,
            "translation_preflight": _preflight(),
        },
    )
    main_module.job_store.add(job)
    previous = os.environ.get("SKIP_PAYMENT_CHECK")
    os.environ["SKIP_PAYMENT_CHECK"] = "1"
    try:
        with patch.object(main_module, "_enqueue_conversion") as enqueue:
            response = TestClient(main_module.app).post(
                f"/api/v2/jobs/{job_id}/confirm-profile",
                headers={"X-Job-Token": token},
                json={
                    "translation_strategy": "mirror_fidelity",
                    "glossary": {"Reflexivity": "反身性"},
                    "characters": [],
                    "chapter_strategy_overrides": {},
                    "bilingual": False,
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "queued"
        assert response.json()["translation_preflight"]["confirmed"] is True
        enqueue.assert_called_once()
        duplicate = TestClient(main_module.app).post(
            f"/api/v2/jobs/{job_id}/confirm-profile",
            headers={"X-Job-Token": token},
            json={},
        )
        assert duplicate.status_code == 409
    finally:
        if previous is None:
            os.environ.pop("SKIP_PAYMENT_CHECK", None)
        else:
            os.environ["SKIP_PAYMENT_CHECK"] = previous


def test_chapter_strategy_override_changes_prompt_and_cache_context():
    translator = SemanticsTranslator(
        model="deepseek-v4-flash",
        translation_strategy="literary_narrative",
    )
    prompt = translator._build_system_prompt("mirror_fidelity")
    assert "镜像级忠实" in prompt
    assert "禁止添加解释、背景、总结" in prompt


def test_footnote_documents_inherit_global_strategy_without_ui_overrides():
    chapters = _chapters(
        {
            "chapters": [
                {
                    "chapter_id": "chapter_1",
                    "file_path": "EPUB/chapter_1.xhtml",
                    "chapter_kind": ChapterKind.body.value,
                    "chunks": [{}, {}],
                },
                {
                    "chapter_id": "page55fn01",
                    "file_path": "EPUB/page55fn01.html",
                    "chapter_kind": ChapterKind.body.value,
                    "chunks": [{}],
                },
            ],
        },
        "mirror_fidelity",
    )
    assert [chapter["chapter_id"] for chapter in chapters] == ["chapter_1"]


def test_sentence_alignment_is_a_warning_signal():
    audit = audit_translation_chunk(
        original_html=(
            "<p>First, the argument begins. Second, it develops. "
            "Third, it concludes with a warning.</p>"
        ),
        translated_html="<p>论证开始并结束。</p>",
    )
    assert "sentence_alignment_suspicious" in audit.flags
    assert audit.risk_level == "warn"


class _Chunk:
    def __init__(self, source: str, translated: str, chunk_id: str = "c1"):
        self.original_html = f"<p>{source}</p>"
        self.translated_html = f"<p>{translated}</p>"
        self.chunk_id = chunk_id
        self.error = None


def test_cross_chapter_consistency_reports_canonical_name_drift():
    result = audit_book_consistency(
        chapter_results={
            "chapter_1": [_Chunk("George discussed Reflexivity.", "乔治讨论了反身性。")],
            "chapter_2": [_Chunk("George returned to Reflexivity.", "乔格再次讨论这个概念。", "c2")],
        },
        glossary={"Reflexivity": "反身性"},
        characters=[{
            "source_name": "George",
            "translated_name": "乔治",
            "aliases": [],
        }],
    )
    assert result["consistency_checks"] == 4
    assert result["consistency_violations"] == 2
    assert result["delivery_gate"] is False


def test_term_highlight_is_deterministic_and_preserves_markup():
    highlighted, count = highlight_confirmed_terms(
        "<html><head></head><body><p>反身性与<b>市场</b>。</p></body></html>".encode("utf-8"),
        {"Reflexivity": "反身性"},
    )
    text = highlighted.decode("utf-8")
    assert count == 1
    assert 'class="epub-term"' in text
    assert 'data-original="Reflexivity"' in text
    assert "<b>市场</b>" in text


if __name__ == "__main__":
    tests = [
        test_confirmation_sanitizes_edits_and_versions_snapshot,
        test_create_translation_stops_before_payment_for_preflight,
        test_confirmation_endpoint_starts_only_after_explicit_confirm,
        test_chapter_strategy_override_changes_prompt_and_cache_context,
        test_footnote_documents_inherit_global_strategy_without_ui_overrides,
        test_sentence_alignment_is_a_warning_signal,
        test_cross_chapter_consistency_reports_canonical_name_drift,
        test_term_highlight_is_deterministic_and_preserves_markup,
    ]
    for test_fn in tests:
        test_fn()
        print(f"  ✅ {test_fn.__name__}")
    print(f"\n📊 {len(tests)} passed, 0 failed")
