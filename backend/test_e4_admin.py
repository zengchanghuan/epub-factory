"""
测试 E4：后台鉴权与管理接口
- E4-1 /api/v2/admin/translation-stats 无密钥时（未配置 ADMIN_SECRET）返回 200
- E4-2 配置 ADMIN_SECRET 后，无密钥返回 403
- E4-3 配置 ADMIN_SECRET 后，正确密钥返回 200
- E4-4 /api/v2/admin/feedback 无数据时返回 200 + 空列表
- E4-5 写入反馈后可通过接口读取
- E4-6 robots.txt 包含 Disallow /admin.html
- E4-7 admin.html 包含 noindex meta 标签
- E4-8 admin.html 包含反馈 Tab 和 localStorage 逻辑
"""
import os
import sys
import tempfile

_tmp_jobs_db = tempfile.mktemp(suffix="_jobs.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_jobs_db}"
os.environ["SKIP_PAYMENT_CHECK"] = "1"
os.environ.pop("ADMIN_SECRET", None)

sys.path.insert(0, os.path.dirname(__file__))

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_e4_1_stats_no_secret():
    r = client.get("/api/v2/admin/translation-stats")
    assert r.status_code == 200, f"未配置密钥时应返回 200，实际 {r.status_code}"


def test_e4_2_stats_with_secret_no_key():
    os.environ["ADMIN_SECRET"] = "test_secret_xyz"
    r = client.get("/api/v2/admin/translation-stats")
    assert r.status_code == 403, f"配置密钥后无 key 应返回 403，实际 {r.status_code}"
    os.environ.pop("ADMIN_SECRET")


def test_e4_3_stats_with_correct_key():
    os.environ["ADMIN_SECRET"] = "test_secret_xyz"
    r = client.get("/api/v2/admin/translation-stats?admin_key=test_secret_xyz")
    assert r.status_code == 200, f"正确密钥应返回 200，实际 {r.status_code}"
    os.environ.pop("ADMIN_SECRET")


def test_e4_4_feedback_empty():
    r = client.get("/api/v2/admin/feedback")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert isinstance(data["items"], list)


def test_e4_5_feedback_submit_and_read():
    r = client.post("/api/v2/feedback", json={
        "job_id": "test123",
        "type": "layout",
        "message": "测试反馈内容"
    })
    assert r.status_code == 200
    assert r.json().get("ok") is True

    r2 = client.get("/api/v2/admin/feedback")
    assert r2.status_code == 200
    items = r2.json().get("items", [])
    assert any(i.get("message") == "测试反馈内容" for i in items), "提交的反馈应能读取到"


def test_e4_6_robots_disallow_admin():
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /admin.html" in r.text, "robots.txt 应屏蔽 /admin.html"


def test_e4_7_admin_html_noindex():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "frontend" / "admin.html").read_text(encoding="utf-8")
    assert "noindex" in html, "admin.html 应包含 noindex meta 标签"


def test_e4_8_admin_html_feedback_tab():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "frontend" / "admin.html").read_text(encoding="utf-8")
    assert "panel-feedback" in html, "应有反馈 Tab 面板"
    assert "localStorage" in html, "应有 localStorage 密钥持久化逻辑"
    assert "feedbackTableBody" in html, "应有反馈数据表格"


# ── 运行 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_e4_1_stats_no_secret,
        test_e4_2_stats_with_secret_no_key,
        test_e4_3_stats_with_correct_key,
        test_e4_4_feedback_empty,
        test_e4_5_feedback_submit_and_read,
        test_e4_6_robots_disallow_admin,
        test_e4_7_admin_html_noindex,
        test_e4_8_admin_html_feedback_tab,
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
    try:
        _os.unlink(_tmp_jobs_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
