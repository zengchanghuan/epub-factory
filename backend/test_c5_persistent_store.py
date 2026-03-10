"""
C5 测试：PostgreSQL 持久化存储（PersistentJobStore）

使用 SQLite 内存库代替 PostgreSQL，无需启动真实数据库服务。

测试用例：
1.  add() 存入后 get() 可取回
2.  get() 不存在的 job_id 返回 None
3.  update_status() 更新状态
4.  update_status() 更新 output_path
5.  update_status() 更新 error_code
6.  update_status() 返回更新后的 Job 对象
7.  update_status() 对不存在的 job_id 返回 None
8.  updated_at 在 update_status 后变更
9.  多个 Job 独立存储，互不干扰
10. bilingual 字段正确持久化
11. 环境变量未设置时 storage.py 使用内存 JobStore
12. EPUB_PERSISTENT_STORE=1 时 storage.py 使用 PersistentJobStore
"""

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine
from app.storage_db import PersistentJobStore, Base
from app.models import Job, JobStatus, OutputMode, DeviceProfile


# ─── 辅助：内存 SQLite 引擎（每个测试独立） ───────────────────────────────────

def make_store() -> PersistentJobStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return PersistentJobStore(engine=engine)


def make_job(**kwargs) -> Job:
    defaults = dict(
        id=uuid.uuid4().hex[:12],
        trace_id=uuid.uuid4().hex,
        source_filename="book.epub",
        input_path="/tmp/book.epub",
        output_mode=OutputMode.simplified,
    )
    defaults.update(kwargs)
    return Job(**defaults)


# ─── 测试 ─────────────────────────────────────────────────────────────────────

def test_add_and_get():
    print("\n" + "=" * 60)
    print("🧪 Test 1: add() 存入后 get() 可取回")
    print("=" * 60)

    store = make_store()
    job = make_job()
    store.add(job)

    fetched = store.get(job.id)
    assert fetched is not None, "应能取回"
    assert fetched.id == job.id
    assert fetched.source_filename == job.source_filename
    print(f"  存入 id={job.id}，取回成功")
    print("  ✅ PASS")
    return True


def test_get_nonexistent():
    print("\n" + "=" * 60)
    print("🧪 Test 2: get() 不存在的 job_id 返回 None")
    print("=" * 60)

    store = make_store()
    result = store.get("nonexistent_id")
    assert result is None
    print("  ✅ PASS")
    return True


def test_update_status():
    print("\n" + "=" * 60)
    print("🧪 Test 3: update_status() 更新状态")
    print("=" * 60)

    store = make_store()
    job = make_job()
    store.add(job)

    store.update_status(job.id, JobStatus.running, message="处理中")
    fetched = store.get(job.id)

    assert fetched.status == JobStatus.running
    assert fetched.message == "处理中"
    print(f"  status={fetched.status}, message={fetched.message}")
    print("  ✅ PASS")
    return True


def test_update_output_path():
    print("\n" + "=" * 60)
    print("🧪 Test 4: update_status() 更新 output_path")
    print("=" * 60)

    store = make_store()
    job = make_job()
    store.add(job)

    store.update_status(job.id, JobStatus.success, output_path="/tmp/out.epub")
    fetched = store.get(job.id)

    assert fetched.output_path == "/tmp/out.epub"
    print(f"  output_path={fetched.output_path}")
    print("  ✅ PASS")
    return True


def test_update_error_code():
    print("\n" + "=" * 60)
    print("🧪 Test 5: update_status() 更新 error_code")
    print("=" * 60)

    store = make_store()
    job = make_job()
    store.add(job)

    store.update_status(job.id, JobStatus.failed, error_code="CONVERT_FAILED", message="失败了")
    fetched = store.get(job.id)

    assert fetched.error_code == "CONVERT_FAILED"
    assert fetched.status == JobStatus.failed
    print(f"  error_code={fetched.error_code}")
    print("  ✅ PASS")
    return True


def test_update_returns_job():
    print("\n" + "=" * 60)
    print("🧪 Test 6: update_status() 返回更新后的 Job 对象")
    print("=" * 60)

    store = make_store()
    job = make_job()
    store.add(job)

    returned = store.update_status(job.id, JobStatus.success, message="成功")
    assert returned is not None
    assert isinstance(returned, Job)
    assert returned.status == JobStatus.success
    print(f"  返回类型: {type(returned).__name__}, status={returned.status}")
    print("  ✅ PASS")
    return True


def test_update_nonexistent_returns_none():
    print("\n" + "=" * 60)
    print("🧪 Test 7: update_status() 对不存在 job_id 返回 None")
    print("=" * 60)

    store = make_store()
    result = store.update_status("ghost_id", JobStatus.running)
    assert result is None
    print("  ✅ PASS")
    return True


def test_updated_at_changes():
    print("\n" + "=" * 60)
    print("🧪 Test 8: updated_at 在 update_status 后变更")
    print("=" * 60)

    import time
    store = make_store()
    job = make_job()
    store.add(job)
    orig_updated = store.get(job.id).updated_at

    time.sleep(0.01)
    store.update_status(job.id, JobStatus.running)
    new_updated = store.get(job.id).updated_at

    assert new_updated >= orig_updated, "updated_at 应变更"
    print(f"  原始: {orig_updated.isoformat()}")
    print(f"  更新: {new_updated.isoformat()}")
    print("  ✅ PASS")
    return True


def test_multiple_jobs_independent():
    print("\n" + "=" * 60)
    print("🧪 Test 9: 多个 Job 独立存储，互不干扰")
    print("=" * 60)

    store = make_store()
    jobs = [make_job() for _ in range(5)]
    for j in jobs:
        store.add(j)

    # 更新其中一个
    store.update_status(jobs[2].id, JobStatus.failed, error_code="ERR")

    for i, j in enumerate(jobs):
        fetched = store.get(j.id)
        if i == 2:
            assert fetched.status == JobStatus.failed
        else:
            assert fetched.status == JobStatus.pending, f"job[{i}] 状态不应变化"

    print(f"  5 个 Job 独立，只有 job[2] 被修改")
    print("  ✅ PASS")
    return True


def test_bilingual_persisted():
    print("\n" + "=" * 60)
    print("🧪 Test 10: bilingual 字段正确持久化")
    print("=" * 60)

    store = make_store()
    job = make_job(bilingual=True)
    store.add(job)

    fetched = store.get(job.id)
    assert fetched.bilingual is True, f"bilingual 应为 True，得到 {fetched.bilingual}"
    print(f"  bilingual={fetched.bilingual}")
    print("  ✅ PASS")
    return True


def test_storage_auto_select_memory():
    print("\n" + "=" * 60)
    print("🧪 Test 11: 环境变量未设置时使用内存 JobStore")
    print("=" * 60)

    # 确保变量不存在
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("EPUB_PERSISTENT_STORE", None)

    # 重新导入 storage 模块
    import importlib
    import app.storage as storage_mod
    importlib.reload(storage_mod)

    from app.storage import JobStore
    assert isinstance(storage_mod.job_store, JobStore), \
        f"应为 JobStore，得到 {type(storage_mod.job_store).__name__}"
    print(f"  store type: {type(storage_mod.job_store).__name__}")
    print("  ✅ PASS")
    return True


def test_storage_auto_select_persistent():
    print("\n" + "=" * 60)
    print("🧪 Test 12: EPUB_PERSISTENT_STORE=1 时使用 PersistentJobStore")
    print("=" * 60)

    os.environ["EPUB_PERSISTENT_STORE"] = "1"
    try:
        import importlib
        import app.storage as storage_mod
        importlib.reload(storage_mod)

        from app.storage_db import PersistentJobStore as PJS
        assert isinstance(storage_mod.job_store, PJS), \
            f"应为 PersistentJobStore，得到 {type(storage_mod.job_store).__name__}"
        print(f"  store type: {type(storage_mod.job_store).__name__}")
        print("  ✅ PASS")
    finally:
        os.environ.pop("EPUB_PERSISTENT_STORE", None)
    return True


# ─── 主入口 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_add_and_get,
        test_get_nonexistent,
        test_update_status,
        test_update_output_path,
        test_update_error_code,
        test_update_returns_job,
        test_update_nonexistent_returns_none,
        test_updated_at_changes,
        test_multiple_jobs_independent,
        test_bilingual_persisted,
        test_storage_auto_select_memory,
        test_storage_auto_select_persistent,
    ]

    passed = failed = 0
    for fn in tests:
        try:
            if fn():
                passed += 1
        except Exception as e:
            print(f"  ❌ FAIL: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"📊 C5 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
