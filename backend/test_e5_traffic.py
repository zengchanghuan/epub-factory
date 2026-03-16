"""
测试 E5：流量统计与功能建议
- E5-1 POST /api/v2/track/pv 成功写入 visits.jsonl
- E5-2 GET /api/v2/admin/visits 返回统计数据
- E5-3 index.html 包含 GA4 代码和功能建议入口
- E5-4 提交 suggestion 类型反馈成功，并能在后台看到
"""
import os
import sys
import json
import tempfile
from pathlib import Path

_tmp_jobs_db = tempfile.mktemp(suffix="_jobs.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_jobs_db}"
os.environ["SKIP_PAYMENT_CHECK"] = "1"

sys.path.insert(0, os.path.dirname(__file__))

from fastapi.testclient import TestClient
from app.main import app, BASE_DIR

client = TestClient(app)

def test_e5_1_track_pv():
    # 确保文件不存在或清空
    pv_file = BASE_DIR / "visits.jsonl"
    if pv_file.exists(): pv_file.unlink()
    
    r = client.post("/api/v2/track/pv", headers={"User-Agent": "TestBot", "X-Real-IP": "1.1.1.1"})
    assert r.status_code == 200
    assert pv_file.exists()
    
    line = pv_file.read_text().strip()
    data = json.loads(line)
    assert data["ip"] == "1.1.1.1"
    assert data["ua"] == "TestBot"

def test_e5_2_admin_visits():
    # 模拟几天的数据
    pv_file = BASE_DIR / "visits.jsonl"
    pv_file.write_text(
        json.dumps({"ts": "2026-03-15T10:00:00Z", "ip": "1.1.1.1", "ua": "X"}) + "\n" +
        json.dumps({"ts": "2026-03-16T10:00:00Z", "ip": "1.1.1.1", "ua": "X"}) + "\n" +
        json.dumps({"ts": "2026-03-16T11:00:00Z", "ip": "2.2.2.2", "ua": "X"}) + "\n"
    )
    
    r = client.get("/api/v2/admin/visits?days=2")
    assert r.status_code == 200
    data = r.json()
    assert data["total_pv"] >= 3
    assert data["total_uv"] >= 2
    assert len(data["daily"]) >= 1

def test_e5_3_frontend_elements():
    html_path = Path(__file__).parent.parent / "frontend" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    assert "googletagmanager" in html
    assert "id=\"suggestBtn\"" in html
    assert "id=\"suggestModal\"" in html

def test_e5_4_suggestion_submission():
    r = client.post("/api/v2/feedback", json={
        "job_id": "general_suggestion",
        "type": "suggestion",
        "message": "I want dark mode!"
    })
    assert r.status_code == 200
    
    r2 = client.get("/api/v2/admin/feedback")
    items = r2.json()["items"]
    assert any(i["type"] == "suggestion" and i["message"] == "I want dark mode!" for i in items)

if __name__ == "__main__":
    tests = [test_e5_1_track_pv, test_e5_2_admin_visits, test_e5_3_frontend_elements, test_e5_4_suggestion_submission]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    
    print(f"\n{'─'*52}\nResults: {passed} passed, {failed} failed\n{'─'*52}")
    if pv_file := (BASE_DIR / "visits.jsonl"): 
        if pv_file.exists(): pv_file.unlink()
    sys.exit(1 if failed else 0)
