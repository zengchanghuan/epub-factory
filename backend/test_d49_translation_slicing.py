"""Offline chapter-boundary slicing: drain paid work and resume durable results."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.cancellation import JobCancelled
import app.domain.fast_translation_runner as runner
from app.domain.translation_continuation import TranslationSliceExpired
from app.engine.cleaners.semantics_translator import SemanticsTranslator, SingleChunkResult
from app.models import Job, JobStatus, OutputMode
from app.storage import JobStore


SOURCES = ("Alpha proposes this approach.", "Beta supports this approach.", "Gamma tests this approach.")
TRANSLATIONS = ("阿尔法提出这种方法。", "贝塔支持这种方法。", "伽马测试这种方法。")


@pytest.fixture
def book(tmp_path, monkeypatch):
    checkpoint_db = str(tmp_path / "checkpoints.db")
    for name, value in {
        "OPENAI_API_KEY": "offline-test", "OPENAI_MODEL": "deepseek-flash",
        "EPUB_DEFAULT_TRANSLATION_MODEL": "deepseek-flash",
        "CELERY_BROKER_URL": "", "REDIS_URL": "",
        "EPUB_LLM_RATE_LIMITER_ENABLED": "0", "EPUB_LLM_GLOBAL_HEALTH_ENABLED": "0",
        "EPUB_TRANSLATION_CHECKPOINT_DB": checkpoint_db,
        "EPUB_TRANSLATION_QUALITY_RETRIES": "1", "EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE": "0",
        "EPUB_CHAPTER_CONCURRENCY": "2", "EPUB_CHAPTER_CONCURRENCY_CAP": "2",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("app.engine.cleaners.semantics_translator.TranslationCache",
                        lambda: Mock(get=Mock(return_value=None), get_latest_compatible=Mock(return_value=None)))
    store = JobStore()
    monkeypatch.setattr(runner, "job_store", store)
    job = Job(id="slice-order", source_filename="book.epub", trace_id="offline",
              input_path=str(tmp_path / "book.epub"), output_mode=OutputMode.simplified,
              enable_translation=True, status=JobStatus.running,
              translation_quality="high", translation_stats={"attempt_id": "same-attempt"})
    store.add(job)
    manifest = {"chapters": [
        {"chapter_id": f"c{i}", "file_path": f"c{i}.xhtml", "chapter_kind": "body",
         "chunks": [{"chunk_id": f"c{i}_0", "sequence": 0,
                     "locator": "/html[1]/body[1]/p[1]", "html": f"<p>{source}</p>",
                     "text": source, "translation_strategy": "html"}]}
        for i, source in enumerate(SOURCES)
    ]}
    content = {chapter["file_path"]: ("<html><body>" + chapter["chunks"][0]["html"] + "</body></html>").encode()
               for chapter in manifest["chapters"]}

    def run(cancel_check=None):
        return asyncio.run(runner._translate_manifest_async(
            job=job, manifest=manifest, content_by_file=content, glossary={},
            progress_callback=lambda _: None, cancel_check=cancel_check))

    return SimpleNamespace(job=job, store=store, run=run, checkpoint_db=checkpoint_db,
                           manifest=manifest, content=content)


def _reply(payload):
    return ({item["id"]: TRANSLATIONS[next(i for i, source in enumerate(SOURCES) if source in item["html"])]
             for item in payload}, {"model": "deepseek-flash", "base_url": "https://mock.invalid", "attempts": 1})


def test_slice_drains_started_chapters_and_resumes_only_remaining_work(book, monkeypatch):
    expired = False
    started, finished, reviewed, cancelled = [], [], [], []
    both_started = None

    async def translate(_translator, payload, **_kwargs):
        nonlocal expired, both_started
        source = payload[0]["html"]
        started.append(source)
        if both_started is None:
            both_started = asyncio.Event()
        if len(started) == 2:
            expired = True
            both_started.set()
        try:
            await both_started.wait()
            await asyncio.sleep(0)
            finished.append(source)
            return _reply(payload)
        except asyncio.CancelledError:
            cancelled.append(source)
            raise

    async def review(_translator, sources, drafts, **_kwargs):
        assert expired  # Paid in-flight chapters finish review after the boundary.
        reviewed.extend(sources)
        await asyncio.sleep(0)
        return drafts

    monkeypatch.setattr(runner, "slice_expired", lambda: expired)
    monkeypatch.setattr(SemanticsTranslator, "_call_llm_json_batch", translate)
    monkeypatch.setattr(SemanticsTranslator, "review_many_chunks_async", review)
    with pytest.raises(TranslationSliceExpired, match="等待自动续跑"):
        book.run()
    assert len(started) == len(finished) == len(reviewed) == 2
    assert not cancelled
    assert all(SOURCES[2] not in source for source in started)
    assert book.store.get(book.job.id).translation_stats["live"] is False
    with sqlite3.connect(book.checkpoint_db) as conn:
        saved = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM book_translation_checkpoints WHERE item_key LIKE 'chunk:%'")]
    assert len(saved) == 2
    assert all(item["phase"] == "final" and not item["result"]["error"] for item in saved)

    # Simulate preprocessing + a huge cached prefix consuming the entire slice.
    # Replay must still reach new work instead of yielding forever without progress.
    expired = True
    new_requests, new_reviews = [], []

    async def resume(_translator, payload, **_kwargs):
        new_requests.extend(item["html"] for item in payload)
        return _reply(payload)

    async def resume_review(_translator, sources, drafts, **_kwargs):
        new_reviews.extend(sources)
        return drafts

    monkeypatch.setattr(SemanticsTranslator, "_call_llm_json_batch", resume)
    monkeypatch.setattr(SemanticsTranslator, "review_many_chunks_async", resume_review)
    stats, _ = book.run()
    assert stats["checkpoint_resumed_chunks"] == 2
    assert stats["failed_chunks"] == 0
    assert len(new_requests) == len(new_reviews) == 1
    assert SOURCES[2] in new_requests[0]
    assert book.store.get(book.job.id).translation_stats["attempt_id"] == "same-attempt"


def test_without_slice_scope_all_chapters_complete(book, monkeypatch):
    requested = []

    async def translate(_translator, payload, **_kwargs):
        requested.extend(item["html"] for item in payload)
        return _reply(payload)

    async def review(_translator, _sources, drafts, **_kwargs):
        return drafts

    monkeypatch.setattr(SemanticsTranslator, "_call_llm_json_batch", translate)
    monkeypatch.setattr(SemanticsTranslator, "review_many_chunks_async", review)
    stats, _ = book.run()
    assert len(requested) == 3
    assert stats["chunks_processed"] == 3
    assert stats["failed_chunks"] == 0


def test_slice_drains_rescue_after_chapter_slot_is_released(book, monkeypatch):
    book.job.translation_quality = "standard"
    monkeypatch.setenv("EPUB_CHAPTER_CONCURRENCY", "1")
    monkeypatch.setenv("EPUB_FAILED_CHUNK_RESCUE", "1")
    expired = False
    calls = []
    chapter = book.manifest["chapters"][0]
    chapter["chunks"].append({"chunk_id": "c0_1", "sequence": 1,
        "locator": "/html[1]/body[1]/p[2]", "html": f"<p>{SOURCES[1]}</p>",
        "text": SOURCES[1], "translation_strategy": "html"})
    book.content[chapter["file_path"]] = ("<html><body>" +
        "".join(chunk["html"] for chunk in chapter["chunks"]) + "</body></html>").encode()

    async def translate(_translator, sources, **kwargs):
        nonlocal expired
        calls.append(sources)
        if len(calls) == 1:
            assert len(sources) == 2 and SOURCES[0] in sources[0]
            expired = True
            results = [SingleChunkResult(TRANSLATIONS[0], False, "mock", "https://mock.invalid", 0, 0, 0, None),
                       SingleChunkResult(sources[1], False, None, None, 0, 0, 0,
                                         "temporary translation failure", retry_count=0)]
        else:
            # Rescue is already-admitted chapter work, even after the slot is free.
            assert expired and len(sources) == 1 and SOURCES[1] in sources[0]
            await asyncio.sleep(0)
            results = [SingleChunkResult(TRANSLATIONS[1], False, "mock", "https://mock.invalid", 0, 0, 0, None)]
        for index, result in enumerate(results):
            kwargs["result_callback"](index, result)
        return results

    monkeypatch.setattr(runner, "slice_expired", lambda: expired)
    monkeypatch.setattr(SemanticsTranslator, "translate_many_chunks_async", translate)
    with pytest.raises(TranslationSliceExpired):
        book.run()
    assert len(calls) == 2
    assert book.store.get(book.job.id).translation_stats["failed_chunk_rescue_succeeded"] == 1
    with sqlite3.connect(book.checkpoint_db) as conn:
        saved = json.loads(conn.execute(
            "SELECT payload FROM book_translation_checkpoints WHERE item_key='chunk:c0_1'").fetchone()[0])
    assert saved["phase"] == "final"
    assert saved["result"]["translated_html"] == TRANSLATIONS[1]
    assert saved["result"]["error"] is None


def test_real_cancellation_takes_priority_over_expired_slice(book, monkeypatch):
    monkeypatch.setattr(runner, "slice_expired", lambda: True)
    with pytest.raises(JobCancelled) as error:
        book.run(cancel_check=lambda: True)
    assert not isinstance(error.value, TranslationSliceExpired)


def test_stats_publish_does_not_swallow_soft_time_limit(book, monkeypatch):
    def interrupted(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(book.store, "update_status", interrupted)
    with pytest.raises(SoftTimeLimitExceeded):
        book.run()


@pytest.mark.parametrize("interruption", (SoftTimeLimitExceeded, asyncio.CancelledError))
def test_interrupted_map_publishes_usage_after_draining_chapters(book, monkeypatch, interruption):
    monkeypatch.setenv("EPUB_CHAPTER_CONCURRENCY", "1")
    monkeypatch.setenv("EPUB_TRANSLATION_STATS_PUBLISH_INTERVAL", "3600")
    incurred = []

    async def interrupted(translator, _payload, **_kwargs):
        # These updates happen after the last throttled live stats publication.
        incurred.append(1)
        translator.stats.api_calls += 7
        translator.stats.prompt_tokens += 11
        translator.stats.completion_tokens += 13
        raise interruption()

    monkeypatch.setattr(SemanticsTranslator, "_call_llm_json_batch", interrupted)
    with pytest.raises(interruption):
        book.run()
    stats = book.store.get(book.job.id).translation_stats
    assert incurred
    assert stats["api_calls"] == 7 * len(incurred)
    assert stats["prompt_tokens"] == 11 * len(incurred)
    assert stats["completion_tokens"] == 13 * len(incurred)
    assert stats["live"] is False
    assert stats["deliverable"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
