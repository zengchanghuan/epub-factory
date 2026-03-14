"""
D2 测试：任务数据模型与建表

测试目标：
1. 新表能被创建：jobs / job_chapters / job_chunks / job_stages / notifications
2. chapter / chunk / stage / notification 能正确持久化
3. chunk upsert 可覆盖更新，支持后续重试与幂等
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine, inspect

from app.models import (
    ChapterKind,
    ChapterStatus,
    ChunkStatus,
    Job,
    JobChapter,
    JobChunk,
    JobNotification,
    JobStage,
    NotificationStatus,
    OutputMode,
    StageStatus,
)
from app.storage_db import Base, PersistentJobStore


def make_store() -> tuple[PersistentJobStore, object]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return PersistentJobStore(engine=engine), engine


def make_job() -> Job:
    return Job(
        id=uuid.uuid4().hex[:12],
        trace_id=uuid.uuid4().hex,
        source_filename="book.epub",
        input_path="/tmp/book.epub",
        output_mode=OutputMode.simplified,
    )


def test_tables_created():
    print("\n" + "=" * 60)
    print("🧪 Test 1: 新表创建成功")
    print("=" * 60)

    store, engine = make_store()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    expected = {"epub_jobs", "job_chapters", "job_chunks", "job_stages", "notifications"}
    missing = expected - tables
    assert not missing, f"缺少表: {missing}"
    print(f"  tables={sorted(tables)}")
    print("  ✅ PASS")
    return True


def test_chapter_persistence():
    print("\n" + "=" * 60)
    print("🧪 Test 2: chapter 可持久化")
    print("=" * 60)

    store, _ = make_store()
    job = make_job()
    store.add(job)
    chapter = JobChapter(
        job_id=job.id,
        chapter_id="chap_01",
        file_path="EPUB/xhtml/01.xhtml",
        chapter_kind=ChapterKind.body,
        status=ChapterStatus.running,
        chunk_total=12,
        chunk_success=4,
        chunk_failed=1,
        chunk_cached=2,
    )
    store.upsert_chapter(chapter)
    rows = store.list_chapters(job.id)
    assert len(rows) == 1
    assert rows[0].chapter_id == "chap_01"
    assert rows[0].chunk_total == 12
    print(f"  chapter={rows[0]}")
    print("  ✅ PASS")
    return True


def test_chunk_upsert():
    print("\n" + "=" * 60)
    print("🧪 Test 3: chunk 可 upsert 更新")
    print("=" * 60)

    store, _ = make_store()
    job = make_job()
    store.add(job)
    chunk = JobChunk(
        job_id=job.id,
        chapter_id="chap_01",
        chunk_id="chunk_001",
        sequence=1,
        locator="/html/body/p[1]",
        source_hash="sha256-1",
        status=ChunkStatus.pending,
    )
    store.upsert_chunk(chunk)
    chunk.status = ChunkStatus.translated
    chunk.prompt_tokens = 12
    chunk.completion_tokens = 18
    chunk.latency_ms = 320
    store.upsert_chunk(chunk)
    rows = store.list_chunks(job.id, "chap_01")
    assert len(rows) == 1
    assert rows[0].status == ChunkStatus.translated
    assert rows[0].prompt_tokens == 12
    assert rows[0].completion_tokens == 18
    print(f"  chunk={rows[0]}")
    print("  ✅ PASS")
    return True


def test_stage_and_notification_persistence():
    print("\n" + "=" * 60)
    print("🧪 Test 4: stage 与 notification 可持久化")
    print("=" * 60)

    store, _ = make_store()
    job = make_job()
    store.add(job)

    stage = JobStage(
        job_id=job.id,
        stage_name="preprocessing",
        status=StageStatus.completed,
        elapsed_ms=1200,
        metadata={"docs": 8},
    )
    notification = JobNotification(
        job_id=job.id,
        channel="in_app",
        status=NotificationStatus.pending,
        payload={"title": "任务已完成"},
    )

    store.add_stage(stage)
    store.add_notification(notification)

    stages = store.list_stages(job.id)
    notifications = store.list_notifications(job.id)
    assert len(stages) == 1
    assert len(notifications) == 1
    assert stages[0].metadata["docs"] == 8
    assert notifications[0].payload["title"] == "任务已完成"
    print(f"  stage={stages[0]}")
    print(f"  notification={notifications[0]}")
    print("  ✅ PASS")
    return True


if __name__ == "__main__":
    tests = [
        test_tables_created,
        test_chapter_persistence,
        test_chunk_upsert,
        test_stage_and_notification_persistence,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as exc:
            failed += 1
            print(f"  ❌ FAIL: {exc}")
        except Exception as exc:
            failed += 1
            print(f"  ❌ ERROR: {exc}")

    print("\n" + "=" * 60)
    print(f"📊 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    raise SystemExit(1 if failed else 0)
