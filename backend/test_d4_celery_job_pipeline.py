"""
D4 测试：整本转换迁入 Celery

- run_conversion 任务已注册
- run_job(job_id) 在 job 不存在时不抛错
- _use_celery() 随环境变量正确返回
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.job_runner import run_job
from app.tasks.job_pipeline import run_conversion


def test_run_conversion_task_registered():
    """jobs.run_conversion 已注册且可调用。"""
    assert run_conversion.name == "jobs.run_conversion"
    # 不真正连 broker，只测 .delay 存在
    assert callable(run_conversion.delay)


def test_run_job_nonexistent_id_no_raise():
    """run_job 对不存在的 job_id 不抛错，仅打 log。"""
    run_job("nonexistent_id_xyz_12345")


def test_use_celery_false_without_env():
    """未设置 REDIS/CELERY_BROKER 时 _use_celery 为 False。"""
    old_redis = os.environ.pop("REDIS_URL", None)
    old_broker = os.environ.pop("CELERY_BROKER_URL", None)
    try:
        from app.main import _use_celery
        assert _use_celery() is False
    finally:
        if old_redis is not None:
            os.environ["REDIS_URL"] = old_redis
        if old_broker is not None:
            os.environ["CELERY_BROKER_URL"] = old_broker


def test_use_celery_true_with_redis_url():
    """设置 REDIS_URL 时 _use_celery 为 True。"""
    from app.main import _use_celery
    os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/0"
    try:
        assert _use_celery() is True
    finally:
        os.environ.pop("REDIS_URL", None)


if __name__ == "__main__":
    tests = [
        test_run_conversion_task_registered,
        test_run_job_nonexistent_id_no_raise,
        test_use_celery_false_without_env,
        test_use_celery_true_with_redis_url,
    ]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  ✅ {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"  ❌ {test.__name__}: {e}")
    print(f"\n📊 {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
