"""
测试 E6：支付守门 API 测试（FastAPI TestClient）

覆盖 3 条核心规则：
- E6-1 开启翻译但未传 paypal_order_id -> 402
- E6-2 开启翻译且订单校验失败 -> 402
- E6-3 关闭翻译（免费任务）-> 200/201（允许创建任务）
"""

import io
import os
import sys
import tempfile

# 使用独立临时数据库，避免污染真实数据
_tmp_rate_db = tempfile.mktemp(suffix="_rate_e6.db")
_tmp_jobs_db = tempfile.mktemp(suffix="_jobs_e6.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_jobs_db}"
os.environ["FREE_DAILY_LIMIT"] = "3"
os.environ["PAYPAL_CLIENT_ID"] = "dummy_client_id_for_test"
os.environ["PAYPAL_SECRET"] = "dummy_secret_for_test"
os.environ.pop("SKIP_PAYMENT_CHECK", None)

sys.path.insert(0, os.path.dirname(__file__))

from app.infra.rate_limiter import RateLimiter
_test_rl = RateLimiter(db_path=_tmp_rate_db)

# 替换全局 rate_limiter 为测试实例，避免受本地真实限流数据影响
import app.infra.rate_limiter as _rl_module
import app.main as _main_module
_rl_module.rate_limiter = _test_rl
_main_module.rate_limiter = _test_rl

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

FAKE_EPUB = b"PK\x03\x04" + b"\x00" * 100
IP_A = "10.10.0.1"
IP_B = "10.10.0.2"
IP_C = "10.10.0.3"


def _post_job(ip: str, *, enable_translation: bool, paypal_order_id: str | None = None):
    data = {
        "output_mode": "simplified",
        "enable_translation": "true" if enable_translation else "false",
        "target_lang": "zh-CN",
        "bilingual": "false",
    }
    if paypal_order_id is not None:
        data["paypal_order_id"] = paypal_order_id
    return client.post(
        "/api/v2/jobs",
        files={"file": ("test.epub", io.BytesIO(FAKE_EPUB), "application/epub+zip")},
        data=data,
        headers={"X-Real-IP": ip},
    )


def test_e6_1_translation_without_order_id_returns_402():
    _test_rl.reset_ip(IP_A)
    r = _post_job(IP_A, enable_translation=True, paypal_order_id=None)
    assert r.status_code == 402, f"未支付应返回 402，实际 {r.status_code}: {r.text}"
    assert "先完成支付" in r.json().get("detail", "")


def test_e6_2_translation_with_invalid_order_returns_402():
    _test_rl.reset_ip(IP_B)
    old_verify = _main_module.verify_paypal_order
    try:
        # 强制模拟 PayPal 校验失败，避免测试依赖外网
        _main_module.verify_paypal_order = lambda order_id, client_id, secret: False
        r = _post_job(IP_B, enable_translation=True, paypal_order_id="FAKE_ORDER_123456")
        assert r.status_code == 402, f"假订单应返回 402，实际 {r.status_code}: {r.text}"
        assert "支付验证失败" in r.json().get("detail", "")
    finally:
        _main_module.verify_paypal_order = old_verify


def test_e6_3_non_translation_job_accepted():
    _test_rl.reset_ip(IP_C)
    r = _post_job(IP_C, enable_translation=False)
    assert r.status_code in (200, 201), f"免费任务应可创建，实际 {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("status") == "queued", f"预期 queued，实际 {body}"
    assert body.get("enable_translation") is False


if __name__ == "__main__":
    tests = [
        test_e6_1_translation_without_order_id_returns_402,
        test_e6_2_translation_with_invalid_order_returns_402,
        test_e6_3_non_translation_job_accepted,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__} [异常]: {e}")
            failed += 1

    print(f"\n{'─'*52}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'─'*52}")

    import os as _os
    for f in [_tmp_rate_db, _tmp_jobs_db]:
        try:
            _os.unlink(f)
        except Exception:
            pass
    sys.exit(1 if failed else 0)

