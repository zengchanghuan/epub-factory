"""
D13 回归与 E2E：上传、排队、状态、通知、下载与失败处理链路。

- E2E：任务被标为「翻译全失败」时，v2 API 必须返回 failed 与 error_code
- E2E：任务结束（成功或失败）后，通知列表应有一条对应记录
- 不依赖真实 EPUB/LLM，用内存 store 与最小 job 完成断言
"""

import os
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from app.main import app
from app.models import Job, JobStatus, OutputMode, DeviceProfile
from app.storage import job_store

client = TestClient(app)

# 最小合法 EPUB 占位（仅用于 POST 创建任务）
MINIMAL_EPUB = b"PK\x03\x04"


def test_e2e_translation_failed_must_return_failed():
    """翻译全失败时必须返回 failed，不得伪装为 success（验收 14.5）。"""
    job_id = f"e2e-fail-{uuid.uuid4().hex[:12]}"
    job = Job(
        id=job_id,
        source_filename="e2e_fail.epub",
        output_mode=OutputMode.simplified,
        trace_id=uuid.uuid4().hex,
        input_path=str(Path(__file__).parent / "uploads" / f"{job_id}.epub"),
        enable_translation=True,
        device=DeviceProfile.generic,
    )
    job_store.add(job)
    job_store.update_status(
        job_id,
        JobStatus.failed,
        message="AI 翻译失败：未成功写入任何译文",
        error_code="TRANSLATION_FAILED",
    )

    get_res = client.get(f"/api/v2/jobs/{job_id}")
    assert get_res.status_code == 200
    out = get_res.json()
    assert out.get("status") == "failed"
    assert out.get("error_code") == "TRANSLATION_FAILED"


def test_e2e_notification_after_job_ends():
    """任务结束后通知链路应有记录（验收 14.5：通知与下载链路联动）。"""
    res = client.post(
        "/api/v2/jobs",
        files={"file": ("e2e_notify.epub", MINIMAL_EPUB, "application/epub+zip")},
        data={"output_mode": "simplified"},
    )
    assert res.status_code == 200
    job_id = res.json().get("job_id")
    assert job_id

    from app.job_runner import run_job
    run_job(job_id)

    list_res = client.get("/api/v2/notifications", params={"job_id": job_id})
    assert list_res.status_code == 200
    items = list_res.json().get("items", [])
    assert len(items) >= 1
    assert any(n.get("job_id") == job_id for n in items)


def _run():
    cases = [
        test_e2e_translation_failed_must_return_failed,
        test_e2e_notification_after_job_ends,
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
