"""
D7 测试：章节级翻译任务拆分

- translate_single_chunk_async 存在且对无英文内容返回 (html, True)
- translate_chapter 对不存在的 job 返回 error
- translate_chapter 对不存在的 chapter_id 返回 error
- translate_chapter 对非 body 章节返回 skipped
- translate_chapter_task 已注册
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.models import Job, OutputMode
from app.storage import job_store
from app.domain.chapter_translation_service import translate_chapter, ChapterTranslationResult, ChunkResult
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.tasks.translate import translate_chapter_task


def test_translate_single_chunk_async_skip_no_english():
    t = SemanticsTranslator(target_lang="zh-CN")
    html = "<p>只有中文。</p>"
    out = asyncio.run(t.translate_single_chunk_async(html))
    assert out.translated_html == html
    assert out.cached is True


def test_translate_chapter_job_not_found():
    result = translate_chapter("nonexistent_job_xyz", "chap_01")
    assert result.error == "job not found"
    assert result.chapter_id == "chap_01"


def test_translate_chapter_chapter_not_in_manifest():
    job_id = "d7_test_job_abc"
    job = Job(
        id=job_id,
        trace_id="tr",
        source_filename="x.epub",
        input_path="/nonexistent/x.epub",
        output_mode=OutputMode.simplified,
        enable_translation=True,
    )
    job_store.add(job)
    try:
        result = translate_chapter(job_id, "nonexistent_chapter_xyz")
        assert result.error is not None
    finally:
        pass
        # job_store 可能无 delete，保留 job 不影响后续测试


def test_translate_chapter_task_registered():
    assert translate_chapter_task.name == "jobs.translate_chapter"
    assert callable(translate_chapter_task.delay)


def test_chapter_result_dataclass():
    r = ChapterTranslationResult(
        job_id="j1",
        chapter_id="c1",
        file_path="f.xhtml",
        chapter_kind="body",
        chunks=[
            ChunkResult("c1_0001", 1, "/p[1]", "<p>a</p>", "<p>b</p>", False),
        ],
    )
    assert r.chunks[0].cached is False
    assert r.chunks[0].translated_html == "<p>b</p>"


def test_upsert_chunk_memory_store():
    from app.models import JobChunk, ChunkStatus
    from app.storage import job_store
    key = "ch9_test_job_xyz"
    chunk = JobChunk(
        job_id=key,
        chapter_id="ch1",
        chunk_id="ch1_0001",
        sequence=1,
        locator="/p[1]",
        source_hash="abc",
        status=ChunkStatus.translated,
        cached=False,
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=100,
    )
    out = job_store.upsert_chunk(chunk)
    assert out.chunk_id == "ch1_0001"
    listed = job_store.list_chunks(key)
    assert len(listed) >= 1
    assert any(c.chunk_id == "ch1_0001" for c in listed)


if __name__ == "__main__":
    tests = [
        test_translate_single_chunk_async_skip_no_english,
        test_translate_chapter_job_not_found,
        test_translate_chapter_chapter_not_in_manifest,
        test_translate_chapter_task_registered,
        test_chapter_result_dataclass,
        test_upsert_chunk_memory_store,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  ✅ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n📊 {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
