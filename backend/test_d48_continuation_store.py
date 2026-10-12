"""Atomic timeout continuation preserves a paid attempt and its saved work."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine

from app.domain.job_dispatch_service import dispatch_pending
from app.models import (
    ChapterStatus, ChunkStatus, Job, JobChapter, JobChunk, JobStatus, OutputMode,
)
from app.storage import JobStore
from app.storage_db import Base, DispatchRecord, PersistentJobStore


@pytest.fixture(params=["memory", "database"])
def store(request, tmp_path):
    if request.param == "memory":
        yield JobStore()
        return
    engine = create_engine(f"sqlite:///{tmp_path / 'continuation.db'}")
    Base.metadata.create_all(engine)
    yield PersistentJobStore(engine)
    engine.dispose()


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.db'}")
    Base.metadata.create_all(engine)
    yield PersistentJobStore(engine)
    engine.dispose()


def add(store, **overrides):
    values = dict(
        id="order", source_filename="book.epub", input_path="/unused/book.epub",
        trace_id="trace", output_mode=OutputMode.simplified,
        enable_translation=True, status=JobStatus.running, expected_amount="59.27",
        translation_model="test-model", translation_quality="high",
        cache_policy="verified", translation_strategy="technical_v1", bilingual=True,
        glossary={"term": "术语"}, metrics_summary="existing usage",
        translation_stats={
            "attempt_id": "attempt", "translation_attempt": 3,
            "restart_count": 2, "free_retry_count": 2, "payment_verified": True,
            "total_chunks": 10, "translated_chunks": 7, "cached_chunks": 1,
            "prompt_tokens": 1234, "completion_tokens": 567, "cost_usd": 0.23,
            "cost_history": [{"attempt_id": "older", "cost_usd": 0.12}],
            "translation_preflight": {"confirmed": True, "strategy": "technical_v1"},
            "checkpoints": {"chapter-1": {"completed": True}},
            "continuation": {"count": 1}, "live": True,
        },
    )
    values.update(overrides)
    running = values["status"] == JobStatus.running
    if running: values["status"] = JobStatus.pending
    store.add(Job(**values))
    if running: assert store.begin_execution("order", "attempt", "executor")
    return store.get("order")


def continue_job(store, **overrides):
    values = dict(
        expected_attempt_id="attempt", continuation_stats={"count": 2, "reason": "time_slice"},
        queued_at=datetime.now(timezone.utc), expected_execution_owner="executor",
    )
    values.update(overrides)
    return store.continue_translation_attempt("order", **values)


def dispatch_snapshot(store):
    from app.domain.dispatch_intent import dispatch_identity
    return store.get_dispatch(dispatch_identity("order", "attempt"))


def test_continuation_preserves_paid_attempt_checkpoints_and_settings(store):
    add(store)
    store.upsert_chapter(JobChapter(
        job_id="order", chapter_id="chapter-1", file_path="chapter.xhtml",
        status=ChapterStatus.completed, chunk_total=1, chunk_success=1,
    ))
    store.upsert_chunk(JobChunk(
        job_id="order", chapter_id="chapter-1", chunk_id="chunk-1", sequence=0,
        locator="p[1]", source_hash="source", translated_text="已完成的译文",
        status=ChunkStatus.translated, prompt_tokens=1234, completion_tokens=567,
    ))
    before = deepcopy(asdict(store.get("order")))
    chapters = deepcopy(store.list_chapters("order"))
    chunks = deepcopy(store.list_chunks("order"))
    continuation = {"count": 2, "progress": {"done": 8}, "attempt_id": "untrusted-nested"}
    job, reason = continue_job(store, continuation_stats=continuation)
    assert reason == "ok"
    assert job.status == JobStatus.pending
    assert job.error_code is None
    assert job.translation_stats["continuation"] == continuation
    assert job.translation_stats["live"] is False
    assert job.translation_stats["deliverable"] is False
    assert job.translation_stats["qa_report"] == {
        "status": "continuing", "summary": "任务已保存进度，正在自动续跑",
        "retryable": False, "free_retry_count": 2,
    }
    for key, value in before["translation_stats"].items():
        if key not in {"continuation", "live", "deliverable", "qa_report"}:
            assert job.translation_stats[key] == value
    after = asdict(job)
    for key, value in before.items():
        if key not in {"status", "message", "error_code", "translation_stats", "updated_at"}:
            assert after[key] == value
    assert store.list_chapters("order") == chapters
    assert store.list_chunks("order") == chunks
    # A mutable caller payload cannot alter the committed checkpoint later.
    continuation["progress"]["done"] = -1
    assert store.get("order").translation_stats["continuation"]["progress"]["done"] == 8


@pytest.mark.parametrize("status", [
    JobStatus.pending, JobStatus.success, JobStatus.failed, JobStatus.cancelled,
    JobStatus.pending_payment, JobStatus.awaiting_confirmation, JobStatus.confirming,
])
def test_continuation_never_reopens_nonrunning_jobs(store, status):
    add(store, status=status)
    before = deepcopy(asdict(store.get("order")))
    _, reason = continue_job(store)
    assert reason == "inactive"
    assert asdict(store.get("order")) == before


@pytest.mark.parametrize("attempt", ["", "old-attempt"])
def test_continuation_requires_exact_nonempty_attempt(store, attempt):
    add(store)
    before = deepcopy(asdict(store.get("order")))
    _, reason = continue_job(store, expected_attempt_id=attempt)
    assert reason == "stale_attempt"
    assert asdict(store.get("order")) == before


def test_continuation_requires_translation_and_existing_job(store):
    assert continue_job(store) == (None, "missing")
    add(store, enable_translation=False)
    before = deepcopy(asdict(store.get("order")))
    _, reason = continue_job(store)
    assert reason == "not_translation"
    assert asdict(store.get("order")) == before


def test_continuation_rejects_stale_snapshot_but_accepts_equivalent_utc_time(store):
    job = add(store)
    updated = job.updated_at.replace(tzinfo=timezone.utc) if job.updated_at.tzinfo is None else job.updated_at
    before = deepcopy(asdict(job))
    _, reason = continue_job(store, expected_updated_at=updated - timedelta(seconds=1))
    assert reason == "stale_state"
    assert asdict(store.get("order")) == before
    _, reason = continue_job(store, expected_updated_at=updated.astimezone(timezone(timedelta(hours=8))))
    assert reason == "ok"


def test_duplicate_continuation_is_idempotent(store):
    add(store)
    assert continue_job(store)[1] == "ok"
    before = deepcopy(asdict(store.get("order")))
    assert continue_job(store, continuation_stats={"count": 99})[1] == "inactive"
    assert asdict(store.get("order")) == before


def test_concurrent_continuations_claim_one_slice(store):
    add(store)
    ready = Barrier(2)

    def queue():
        ready.wait(timeout=5)
        return continue_job(store)[1]

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(queue) for _ in range(2)]
        assert sorted(result.result(timeout=5) for result in results) == ["inactive", "ok"]
    assert store.get("order").translation_stats["continuation"]["count"] == 2


def test_cancel_racing_continuation_cannot_reopen_job(store):
    add(store)
    ready = Barrier(2)

    def queue():
        ready.wait(timeout=5)
        return continue_job(store)[1]

    def cancel():
        ready.wait(timeout=5)
        store.update_status("order", JobStatus.cancelled, "用户停止")

    with ThreadPoolExecutor(max_workers=2) as workers:
        queued = workers.submit(queue)
        cancelled = workers.submit(cancel)
        assert queued.result(timeout=5) in {"ok", "inactive"}
        cancelled.result(timeout=5)
    assert store.get("order").status == JobStatus.cancelled
    enqueue = Mock()
    dispatch_pending(store, enqueue, job_id="order")
    enqueue.assert_not_called()


def test_invalid_continuation_payload_does_not_change_job(store):
    add(store)
    before = deepcopy(asdict(store.get("order")))
    with pytest.raises(TypeError):
        continue_job(store, continuation_stats=["not", "metadata"])
    assert asdict(store.get("order")) == before


def test_continuation_and_dispatch_rollback_together(database):
    add(database)
    before = asdict(database.get("order"))
    previous_dispatch = dispatch_snapshot(database)
    previous_execution = database.get_execution("order", "attempt")
    from sqlalchemy.orm import Session
    with patch.object(Session, "commit", side_effect=RuntimeError("simulated disk full")):
        with pytest.raises(RuntimeError, match="simulated disk full"):
            continue_job(database)
    assert asdict(database.get("order")) == before
    assert dispatch_snapshot(database) == previous_dispatch
    assert database.get_execution("order", "attempt") == previous_execution


def test_continuation_rearms_same_outbox_and_releases_executor(store):
    add(store)
    assert continue_job(store)[1] == "ok"
    assert store.get_execution("order", "attempt")["state"] == "queued"
    assert store.get_execution("order", "attempt")["owner"] == ""
    intent = dispatch_snapshot(store)
    assert intent["status"] == "pending" and not intent["lease_token"]
    enqueue = Mock()
    dispatch_pending(store, enqueue, job_id="order")
    enqueue.assert_called_once_with("order", "attempt")
    assert store.begin_execution("order", "attempt", "next-executor")
    assert continue_job(store)[1] == "stale_owner"


def test_continuation_rejects_old_execution_owner(store):
    add(store)
    before = asdict(store.get("order"))
    assert continue_job(store, expected_execution_owner="obsolete")[1] == "stale_owner"
    assert asdict(store.get("order")) == before


def test_old_dispatcher_cannot_acknowledge_a_continued_slice(store):
    add(store, status=JobStatus.pending)
    old = store.claim_dispatch(job_id="order")
    assert store.begin_execution("order", "attempt", "executor")
    assert continue_job(store)[1] == "ok"
    assert not store.finish_dispatch(old["dispatch_id"], old["lease_token"])
    enqueue = Mock()
    dispatch_pending(store, enqueue, job_id="order")
    enqueue.assert_called_once_with("order", "attempt")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
