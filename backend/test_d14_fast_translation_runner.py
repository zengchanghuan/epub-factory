"""
D14 测试：快速翻译主链路

- 预处理后走 MapReduce 翻译
- 所有 chunk 使用同一份 glossary
- 译后术语审计能把仍保留的英文术语修正为指定译名
"""

import sys
import tempfile
import time
import uuid
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ebooklib import epub

from app.cancellation import JobCancelled
import app.engine.glossary_service as glossary_service
from app.domain.fast_translation_runner import (
    _translation_delivery_gate_result,
    _translation_failures_exceed_delivery_gate,
    run_fast_translation_job,
)
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.models import ChunkStatus, ConversionResult, DeviceProfile, ErrorCode, Job, OutputMode
from app.storage import job_store


def _make_epub(path: Path, marker: str) -> None:
    book = epub.EpubBook()
    book.set_identifier(f"d14-{marker}")
    book.set_title("The Annotated and Illustrated Double Helix")
    book.set_language("en")
    titlepage = epub.EpubHtml(title="Title Page", file_name="titlepage.xhtml", lang="en")
    titlepage.content = (
        "<html><head><title>The Annotated and Illustrated Double Helix</title></head>"
        "<body><h1>The Annotated and Illustrated Double Helix</h1></body></html>"
    )
    chapter = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
    chapter.content = (
        "<html><body>"
        f"<h1>Chapter 1</h1><p>Mr. Smith travels to London with Watson and Franklin {marker}.</p>"
        "</body></html>"
    )
    book.add_item(titlepage)
    book.add_item(chapter)
    book.spine.append(titlepage)
    book.spine.append(chapter)
    book.toc = (epub.Link("chap_01.xhtml", "Chapter 1", "chap1"),)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book, {})


def test_fast_translation_runner_glossary_audit():
    marker = uuid.uuid4().hex[:10]
    old_translate_glossary = glossary_service.translate_glossary
    old_call = SemanticsTranslator._call_llm_json_batch

    async def fake_translate_glossary(*args, **kwargs):
        return {}

    glossary_service.translate_glossary = fake_translate_glossary

    async def fake_call(self, payload):
        # 故意保留 Smith/London，验证 verify_and_fix 会按 glossary 修正。
        if any("Annotated and Illustrated Double Helix" in item["html"] for item in payload):
            return (
                {item["id"]: "注释图解版《双螺旋》" for item in payload},
                {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 20, "completion_tokens": 30},
            )
        return (
            {
                item["id"]: item["html"].replace(
                    "Mr. Smith travels to London with Watson and Franklin",
                    "Smith 到访 London，与 Watson 和 Franklin 同行",
                )
                for item in payload
            },
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 20, "completion_tokens": 30},
        )

    SemanticsTranslator._call_llm_json_batch = fake_call
    try:
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "input.epub"
            out = Path(tmp) / "output.epub"
            _make_epub(inp, marker)
            job = Job(
                id=f"d14_{marker}",
                source_filename="input.epub",
                output_mode=OutputMode.simplified,
                trace_id=uuid.uuid4().hex,
                input_path=str(inp),
                enable_translation=True,
                target_lang="zh-CN",
                glossary={"Smith": "史密斯", "London": "伦敦"},
                device=DeviceProfile.generic,
            )
            result = run_fast_translation_job(
                job=job,
                input_path=inp,
                output_path=out,
                progress_callback=lambda _msg: None,
                stage_callback=lambda _stage, _msg, _elapsed=None: None,
            )
            assert out.is_file()
            assert result.translation_stats["translated_chunks"] >= 1
            assert result.translation_stats["glossary_fixed_count"] >= 2
            assert "audit_warn_chunks" in result.translation_stats
            assert "audit_failed_chunks" in result.translation_stats
            assert "audit_flags_count" in result.translation_stats
            assert result.translation_stats["book_title_original"] == "The Annotated and Illustrated Double Helix"
            assert result.translation_stats["book_title_translated"] == "注释图解版《双螺旋》"
            chunks = job_store.list_chunks(job.id)
            assert chunks
            assert any(c.source_text for c in chunks)
            assert any(c.translated_text for c in chunks)
            assert all(isinstance(c.audit_json, dict) for c in chunks)
            out_book = epub.read_epub(str(out))
            titles = out_book.get_metadata("DC", "title")
            assert titles and titles[0][0] == "注释图解版《双螺旋》"
            combined = []
            for item in out_book.get_items():
                if item.get_type() == 9:
                    content = item.get_content()
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="ignore")
                    combined.append(str(content))
            text = "\n".join(combined)
            assert "注释图解版《双螺旋》" in text
            assert "史密斯" in text
            assert "伦敦" in text
    finally:
        glossary_service.translate_glossary = old_translate_glossary
        SemanticsTranslator._call_llm_json_batch = old_call


def test_translation_failure_delivery_gate():
    assert _translation_failures_exceed_delivery_gate({
        "failed_chunks": 546,
        "total_chunks": 2414,
    }) is True
    assert _translation_failures_exceed_delivery_gate({
        "failed_chunks": 3,
        "total_chunks": 2414,
    }) is False
    assert _translation_failures_exceed_delivery_gate({
        "failed_chunks": 30,
        "total_chunks": 10000,
    }) is False


def test_delivery_gate_result_keeps_retryable_qa_report():
    stats = {
        "model": "deepseek-v4-flash",
        "total_chunks": 2414,
        "translated_chunks": 2300,
        "cached_chunks": 16,
        "failed_chunks": 98,
        "last_error": "untranslated response; retry still invalid",
        "audit_flags_count": {"likely_untranslated": 98},
    }
    progress = []

    result = _translation_delivery_gate_result(
        pre_result=ConversionResult(),
        translation_stats=stats,
        timings=[],
        started_all=time.monotonic(),
        failed=98,
        total=2414,
        last_error=stats["last_error"],
        progress_callback=progress.append,
    )

    assert result.validation_passed is False
    assert result.error_code == ErrorCode.PARTIAL_TRANSLATION.value
    assert result.translation_stats["delivery_gate_failed"] is True
    assert result.translation_stats["deliverable"] is False
    assert result.translation_stats["qa_report"]["status"] == "failed"
    assert result.translation_stats["qa_report"]["retryable"] is True
    assert "output_missing" not in result.translation_stats["qa_report"]["flags"]
    assert "98/2414" in result.message
    assert any("翻译交付质检未通过" in msg for msg in progress)


def test_fast_translation_runner_rescues_failed_chunk_queue():
    marker = uuid.uuid4().hex[:10]
    old_call = SemanticsTranslator._call_llm_json_batch
    old_translate_glossary = glossary_service.translate_glossary
    old_env = {
        "EPUB_TRANSLATION_QUALITY_RETRIES": os.environ.get("EPUB_TRANSLATION_QUALITY_RETRIES"),
        "EPUB_FAILED_CHUNK_RESCUE": os.environ.get("EPUB_FAILED_CHUNK_RESCUE"),
        "EPUB_FAILED_CHUNK_RESCUE_CONCURRENCY": os.environ.get("EPUB_FAILED_CHUNK_RESCUE_CONCURRENCY"),
        "EPUB_FAILED_CHUNK_ARCHIVE": os.environ.get("EPUB_FAILED_CHUNK_ARCHIVE"),
        "EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE": os.environ.get("EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE"),
    }
    attempts = {"body": 0}

    async def fake_translate_glossary(*_args, **_kwargs):
        return {}

    async def fake_call(self, payload, **_kwargs):
        translations = {}
        for item in payload:
            item_id = int(item["id"])
            source_html = item["html"]
            if "Annotated and Illustrated Double Helix" in source_html:
                translations[item_id] = "注释图解版《双螺旋》"
            elif marker in source_html:
                attempts["body"] += 1
                if attempts["body"] <= 2:
                    translations[item_id] = source_html
                else:
                    translations[item_id] = f"史密斯补译后前往伦敦 {marker}。"
            elif "Chapter 1" in source_html:
                translations[item_id] = "第一章 1"
            else:
                translations[item_id] = "已翻译"
        return (
            translations,
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 20, "completion_tokens": 30},
        )

    SemanticsTranslator._call_llm_json_batch = fake_call
    glossary_service.translate_glossary = fake_translate_glossary
    os.environ["EPUB_TRANSLATION_QUALITY_RETRIES"] = "1"
    os.environ["EPUB_FAILED_CHUNK_RESCUE"] = "1"
    os.environ["EPUB_FAILED_CHUNK_RESCUE_CONCURRENCY"] = "1"
    os.environ["EPUB_FAILED_CHUNK_ARCHIVE"] = "0"
    os.environ["EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE"] = "0"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "input.epub"
            out = Path(tmp) / "output.epub"
            _make_epub(inp, marker)
            job = Job(
                id=f"d14_rescue_{marker}",
                source_filename="input.epub",
                output_mode=OutputMode.simplified,
                trace_id=uuid.uuid4().hex,
                input_path=str(inp),
                enable_translation=True,
                target_lang="zh-CN",
                device=DeviceProfile.generic,
            )

            result = run_fast_translation_job(
                job=job,
                input_path=inp,
                output_path=out,
                progress_callback=lambda _msg: None,
                stage_callback=lambda _stage, _msg, _elapsed=None: None,
            )

            assert out.is_file()
            assert attempts["body"] >= 3
            assert result.translation_stats["failed_chunk_rescue_candidates"] >= 1
            assert result.translation_stats["failed_chunk_rescue_attempted"] >= 1
            assert result.translation_stats["failed_chunk_rescue_succeeded"] >= 1
            assert result.translation_stats["failed_chunks"] == 0
            chunks = job_store.list_chunks(job.id)
            rescued = [c for c in chunks if marker in c.source_text]
            assert rescued
            assert rescued[0].status == ChunkStatus.translated
            assert rescued[0].error_message is None

            out_book = epub.read_epub(str(out))
            combined = []
            for item in out_book.get_items():
                if item.get_type() == 9:
                    content = item.get_content()
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="ignore")
                    combined.append(str(content))
            assert f"史密斯补译后前往伦敦 {marker}" in "\n".join(combined)
    finally:
        SemanticsTranslator._call_llm_json_batch = old_call
        glossary_service.translate_glossary = old_translate_glossary
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_fast_translation_runner_keeps_failed_chunk_after_rescue_queue_exhausted():
    marker = uuid.uuid4().hex[:10]
    old_call = SemanticsTranslator._call_llm_json_batch
    old_translate_glossary = glossary_service.translate_glossary
    old_env = {
        "EPUB_TRANSLATION_QUALITY_RETRIES": os.environ.get("EPUB_TRANSLATION_QUALITY_RETRIES"),
        "EPUB_FAILED_CHUNK_RESCUE": os.environ.get("EPUB_FAILED_CHUNK_RESCUE"),
        "EPUB_FAILED_CHUNK_RESCUE_CONCURRENCY": os.environ.get("EPUB_FAILED_CHUNK_RESCUE_CONCURRENCY"),
        "EPUB_FAILED_CHUNK_ARCHIVE": os.environ.get("EPUB_FAILED_CHUNK_ARCHIVE"),
        "EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE": os.environ.get("EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE"),
    }
    attempts = {"body": 0}

    async def fake_translate_glossary(*_args, **_kwargs):
        return {}

    async def fake_call(self, payload, **_kwargs):
        translations = {}
        for item in payload:
            item_id = int(item["id"])
            source_html = item["html"]
            if "Annotated and Illustrated Double Helix" in source_html:
                translations[item_id] = "注释图解版《双螺旋》"
            elif marker in source_html:
                attempts["body"] += 1
                translations[item_id] = source_html
            elif "Chapter 1" in source_html:
                translations[item_id] = "第一章 1"
            else:
                translations[item_id] = "已翻译"
        return (
            translations,
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 20, "completion_tokens": 30},
        )

    SemanticsTranslator._call_llm_json_batch = fake_call
    glossary_service.translate_glossary = fake_translate_glossary
    os.environ["EPUB_TRANSLATION_QUALITY_RETRIES"] = "1"
    os.environ["EPUB_FAILED_CHUNK_RESCUE"] = "1"
    os.environ["EPUB_FAILED_CHUNK_RESCUE_CONCURRENCY"] = "1"
    os.environ["EPUB_FAILED_CHUNK_ARCHIVE"] = "0"
    os.environ["EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE"] = "0"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "input.epub"
            out = Path(tmp) / "output.epub"
            _make_epub(inp, marker)
            job = Job(
                id=f"d14_rescue_fail_{marker}",
                source_filename="input.epub",
                output_mode=OutputMode.simplified,
                trace_id=uuid.uuid4().hex,
                input_path=str(inp),
                enable_translation=True,
                target_lang="zh-CN",
                device=DeviceProfile.generic,
            )

            result = run_fast_translation_job(
                job=job,
                input_path=inp,
                output_path=out,
                progress_callback=lambda _msg: None,
                stage_callback=lambda _stage, _msg, _elapsed=None: None,
            )

            assert out.is_file()
            assert attempts["body"] >= 3
            assert result.error_code == ErrorCode.PARTIAL_TRANSLATION.value
            assert result.translation_stats["failed_chunk_rescue_candidates"] >= 1
            assert result.translation_stats["failed_chunk_rescue_attempted"] >= 1
            assert result.translation_stats["failed_chunk_rescue_failed"] >= 1
            assert result.translation_stats["failed_chunks"] >= 1
            chunks = job_store.list_chunks(job.id)
            failed = [c for c in chunks if marker in c.source_text]
            assert failed
            assert failed[0].status == ChunkStatus.failed
            assert failed[0].error_message
    finally:
        SemanticsTranslator._call_llm_json_batch = old_call
        glossary_service.translate_glossary = old_translate_glossary
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_fast_translation_runner_honors_cancel_check_before_work():
    marker = uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory() as tmp:
        inp = Path(tmp) / "input.epub"
        out = Path(tmp) / "output.epub"
        _make_epub(inp, marker)
        job = Job(
            id=f"d14_cancel_{marker}",
            source_filename="input.epub",
            output_mode=OutputMode.simplified,
            trace_id=uuid.uuid4().hex,
            input_path=str(inp),
            enable_translation=True,
            target_lang="zh-CN",
            device=DeviceProfile.generic,
        )
        try:
            run_fast_translation_job(
                job=job,
                input_path=inp,
                output_path=out,
                progress_callback=lambda _msg: None,
                stage_callback=lambda _stage, _msg, _elapsed=None: None,
                cancel_check=lambda: True,
            )
        except JobCancelled:
            pass
        else:
            raise AssertionError("expected JobCancelled")

        assert not out.exists()


if __name__ == "__main__":
    tests = [
        test_fast_translation_runner_glossary_audit,
        test_translation_failure_delivery_gate,
        test_delivery_gate_result_keeps_retryable_qa_report,
        test_fast_translation_runner_rescues_failed_chunk_queue,
        test_fast_translation_runner_keeps_failed_chunk_after_rescue_queue_exhausted,
        test_fast_translation_runner_honors_cancel_check_before_work,
    ]
    passed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  ❌ {fn.__name__}: {exc}")
            raise
    print(f"\n📊 {passed} passed, 0 failed")
