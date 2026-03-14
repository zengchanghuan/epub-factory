"""
D1 测试：Celery 基础设施起盘

测试目标：
1. 能从环境变量构建 Celery app
2. Broker / Result Backend 配置正确
3. health task 已注册并可直接执行
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.infra.celery_app import build_celery_app
from app.tasks.health import ping


def test_build_celery_app_from_env():
    print("\n" + "=" * 60)
    print("🧪 Test 1: Celery app 从环境变量构建")
    print("=" * 60)

    old_broker = os.environ.get("CELERY_BROKER_URL")
    old_backend = os.environ.get("CELERY_RESULT_BACKEND")
    os.environ["CELERY_BROKER_URL"] = "redis://127.0.0.1:6379/9"
    os.environ["CELERY_RESULT_BACKEND"] = "redis://127.0.0.1:6379/10"
    try:
        app = build_celery_app()
        assert app.conf.broker_url == "redis://127.0.0.1:6379/9"
        assert app.conf.result_backend == "redis://127.0.0.1:6379/10"
        print(f"  broker={app.conf.broker_url}")
        print(f"  backend={app.conf.result_backend}")
        print("  ✅ PASS")
        return True
    finally:
        if old_broker is None:
            os.environ.pop("CELERY_BROKER_URL", None)
        else:
            os.environ["CELERY_BROKER_URL"] = old_broker
        if old_backend is None:
            os.environ.pop("CELERY_RESULT_BACKEND", None)
        else:
            os.environ["CELERY_RESULT_BACKEND"] = old_backend


def test_health_task_registered_and_runnable():
    print("\n" + "=" * 60)
    print("🧪 Test 2: health task 已注册并可直接执行")
    print("=" * 60)

    assert ping.name == "infra.health.ping"
    result = ping.run()
    assert result["status"] == "ok"
    assert result["service"] == "celery"
    assert "timestamp" in result
    print(f"  task={ping.name}")
    print(f"  result={result}")
    print("  ✅ PASS")
    return True


if __name__ == "__main__":
    tests = [
        test_build_celery_app_from_env,
        test_health_task_registered_and_runnable,
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
