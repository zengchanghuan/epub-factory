"""
D11 测试：通知系统第一版

- 内存 store add_notification / list_notifications
- notify_job_completed 写入站内通知且可被 list 查到
- GET /api/v2/notifications 返回 items（含 payload）
"""

import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from app.models import JobNotification, JobStatus, NotificationStatus
from app.storage import job_store
from app.domain.notification_service import notify_job_completed, CHANNEL_IN_APP
from app.main import app


def test_add_and_list_notifications():
    job_id = "d11_job_1"
    n = JobNotification(
        job_id=job_id,
        channel=CHANNEL_IN_APP,
        status=NotificationStatus.sent,
        payload={"status": "success", "message": "完成"},
        created_at=datetime.now(timezone.utc),
    )
    job_store.add_notification(n)
    listed = job_store.list_notifications(job_id=job_id)
    assert len(listed) >= 1
    assert any(x.job_id == job_id and x.channel == CHANNEL_IN_APP for x in listed)


def test_notify_job_completed_adds_in_app():
    job_id = "d11_job_2"
    # 先塞一个假 job 进 store，否则 notify 里不依赖 job 存在
    notify_job_completed(
        job_id=job_id,
        status=JobStatus.success,
        message="转换成功",
        output_path="/tmp/out.epub",
        source_filename="test.epub",
    )
    listed = job_store.list_notifications(job_id=job_id)
    assert len(listed) >= 1
    n = next(x for x in listed if x.job_id == job_id and x.channel == CHANNEL_IN_APP)
    assert n.payload.get("status") == "success"
    assert n.payload.get("output_path") == "/tmp/out.epub"
    assert n.status == NotificationStatus.sent


def test_list_notifications_filter_by_job_id():
    job_store.add_notification(JobNotification(
        job_id="j_a",
        channel=CHANNEL_IN_APP,
        status=NotificationStatus.sent,
        payload={},
        created_at=datetime.now(timezone.utc),
    ))
    job_store.add_notification(JobNotification(
        job_id="j_b",
        channel=CHANNEL_IN_APP,
        status=NotificationStatus.sent,
        payload={},
        created_at=datetime.now(timezone.utc),
    ))
    a_list = job_store.list_notifications(job_id="j_a")
    assert all(n.job_id == "j_a" for n in a_list)
    all_list = job_store.list_notifications(job_id=None)
    assert len(all_list) >= 2


def test_v2_notifications_api_returns_items():
    """GET /api/v2/notifications 返回 items 数组；有通知时含 payload。"""
    client = TestClient(app)
    notify_job_completed(
        "d11_api_job",
        JobStatus.success,
        "完成",
        source_filename="api_test.epub",
    )
    res = client.get("/api/v2/notifications", params={"job_id": "d11_api_job"})
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert isinstance(data["items"], list)
    assert len(data["items"]) >= 1
    item = next((x for x in data["items"] if x.get("job_id") == "d11_api_job"), None)
    assert item is not None
    assert item.get("channel") == CHANNEL_IN_APP
    assert "payload" in item and item["payload"].get("status") == "success"


def _run():
    cases = [
        test_add_and_list_notifications,
        test_notify_job_completed_adds_in_app,
        test_list_notifications_filter_by_job_id,
        test_v2_notifications_api_returns_items,
    ]
    passed = 0
    for fn in cases:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {fn.__name__}: {e}")
            raise
    print(f"\n📊 {passed} passed, 0 failed")


if __name__ == "__main__":
    _run()
