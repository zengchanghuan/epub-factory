"""
D5 测试：阶段状态与结构化日志

- 转换过程中 stage_callback 被调用，阶段写入 store
- GET /api/v2/jobs/{id}/events 返回包含 stage 与 message 的 items
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from app.main import app
from app.models import Job, JobStage, OutputMode, StageStatus
from app.storage import job_store
from app.job_runner import run_job

client = TestClient(app)
MINIMAL_EPUB = b"PK\x03\x04"


def test_stage_events_recorded_on_run():
    """run_job 执行时 stage_callback 写入 store，list_stages 可查到。"""
    job_id = uuid.uuid4().hex[:12]
    input_path = Path(__file__).parent / "uploads" / f"{job_id}-stage_test.epub"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(MINIMAL_EPUB)
    try:
        job = Job(
            id=job_id,
            trace_id=uuid.uuid4().hex,
            source_filename="stage_test.epub",
            input_path=str(input_path),
            output_mode=OutputMode.simplified,
        )
        job_store.add(job)
        run_job(job_id)
        stages = job_store.list_stages(job_id)
        assert len(stages) >= 1, "至少应有一条阶段记录（如 preprocessing 开始解包）"
        names = {s.stage_name for s in stages}
        assert "preprocessing" in names
        messages = [s.metadata.get("message", "") for s in stages if s.metadata]
        assert any("解包" in m for m in messages), "应有解包相关 message"
    finally:
        if input_path.exists():
            input_path.unlink(missing_ok=True)


def test_v2_events_api_returns_stage_items():
    """GET /api/v2/jobs/{id}/events 返回 items，每项含 time/level/stage/message。"""
    res = client.post(
        "/api/v2/jobs",
        files={"file": ("events_stage.epub", MINIMAL_EPUB, "application/epub+zip")},
        data={"output_mode": "simplified"},
    )
    assert res.status_code == 200
    created = res.json()
    job_id = created["job_id"]
    res = client.get(f"/api/v2/jobs/{job_id}/events", params={"token": created["access_token"]})
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    items = data["items"]
    if items:
        one = items[0]
        assert "time" in one
        assert "stage" in one
        assert "message" in one


def test_v2_events_exposes_metadata_level():
    """GET /events 应透出 progress/error level，方便 UI 直接展示诊断日志。"""
    job_id = uuid.uuid4().hex[:12]
    job = Job(
        id=job_id,
        trace_id=uuid.uuid4().hex,
        source_filename="events_level.epub",
        input_path="/tmp/events_level.epub",
        output_mode=OutputMode.simplified,
        access_token="events-level-token",
    )
    job_store.add(job)
    now = datetime.now(timezone.utc)
    job_store.add_stage(JobStage(
        job_id=job_id,
        stage_name="translation_quality_gate_failed",
        status=StageStatus.completed,
        started_at=now,
        finished_at=now,
        metadata={"message": "翻译交付质检未通过：仍有 98/2414 个段落失败", "level": "error"},
    ))

    res = client.get(f"/api/v2/jobs/{job_id}/events", params={"token": "events-level-token"})
    assert res.status_code == 200
    items = res.json()["items"]
    assert items[-1]["level"] == "error"
    assert "翻译交付质检未通过" in items[-1]["message"]


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in [
        ("stage_events_recorded", test_stage_events_recorded_on_run),
        ("v2_events_api", test_v2_events_api_returns_stage_items),
        ("v2_events_metadata_level", test_v2_events_exposes_metadata_level),
    ]:
        try:
            fn()
            passed += 1
            print(f"  ✅ {name}")
        except Exception as e:
            failed += 1
            print(f"  ❌ {name}: {e}")
    print(f"\n📊 {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
