"""
A2 E2E 测试：前后端完整流程验证

使用 FastAPI TestClient（基于 httpx），无需启动真实服务器。
测试用例：
1. 上传 EPUB → 创建 job → 返回 job_id
2. 轮询 job 直到 success → 验证所有字段
3. 下载结果 → 验证是有效 EPUB（ZIP 且含 mimetype）
4. 上传非法文件 → 返回 400
5. device 参数传递正确（kindle/apple/generic）
6. 查询不存在的 job → 返回 404
"""

import os
import sys
import time
import zipfile
import tempfile
from pathlib import Path

os.environ["EPUBCHECK_JAR"] = "/Users/zengchanghuan/Desktop/workspace/epub-factory/tools/epubcheck-5.1.0/epubcheck.jar"

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

TEST_EPUB = "/Users/zengchanghuan/Desktop/workspace/epub-factory/backend/test_en.epub"


def wait_for_job(job_id: str, timeout: int = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = client.get(f"/api/v1/jobs/{job_id}")
        assert res.status_code == 200
        data = res.json()
        if data["status"] in ("success", "failed"):
            return data
        time.sleep(0.5)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


def test_healthz():
    print("\n" + "=" * 60)
    print("🧪 Test 1: 健康检查")
    print("=" * 60)
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    print("  ✅ PASS")
    return True


def test_upload_and_complete():
    print("\n" + "=" * 60)
    print("🧪 Test 2: 上传 EPUB → 轮询 → 成功")
    print("=" * 60)

    if not Path(TEST_EPUB).exists():
        print("  ⏭️ SKIP: test_en.epub not found")
        return True

    with open(TEST_EPUB, "rb") as f:
        res = client.post(
            "/api/v1/jobs",
            files={"file": ("test_book.epub", f, "application/epub+zip")},
            data={"output_mode": "simplified", "device": "generic"},
        )

    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert "job_id" in data
    assert data["status"] == "pending"
    assert data["device"] == "generic"
    print(f"  Job created: {data['job_id']}")

    result = wait_for_job(data["job_id"])
    assert result["status"] == "success", f"Job failed: {result.get('message')}"
    assert result["download_url"] is not None
    print(f"  Job completed: {result['status']}")
    print(f"  Download URL: {result['download_url']}")
    print("  ✅ PASS")
    return True


def test_download_valid_epub():
    print("\n" + "=" * 60)
    print("🧪 Test 3: 下载结果是有效 EPUB")
    print("=" * 60)

    if not Path(TEST_EPUB).exists():
        print("  ⏭️ SKIP")
        return True

    with open(TEST_EPUB, "rb") as f:
        res = client.post(
            "/api/v1/jobs",
            files={"file": ("download_test.epub", f, "application/epub+zip")},
            data={"output_mode": "traditional", "device": "kindle"},
        )

    data = res.json()
    result = wait_for_job(data["job_id"])
    assert result["status"] == "success"

    dl_res = client.get(result["download_url"])
    assert dl_res.status_code == 200
    assert len(dl_res.content) > 1000, f"Downloaded file too small: {len(dl_res.content)} bytes"

    tmp = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    tmp.write(dl_res.content)
    tmp.close()

    try:
        assert zipfile.is_zipfile(tmp.name), "Downloaded file is not a valid ZIP"
        with zipfile.ZipFile(tmp.name, "r") as zf:
            names = zf.namelist()
            assert "mimetype" in names, "EPUB missing mimetype entry"
            mimetype = zf.read("mimetype").decode().strip()
            assert mimetype == "application/epub+zip", f"Wrong mimetype: {mimetype}"
            xhtml_files = [n for n in names if n.endswith('.xhtml')]
            assert len(xhtml_files) > 0, "No XHTML files in EPUB"
        print(f"  Valid EPUB: {len(dl_res.content)} bytes, {len(xhtml_files)} XHTML files")
    finally:
        os.unlink(tmp.name)

    print("  ✅ PASS")
    return True


def test_reject_invalid_file():
    print("\n" + "=" * 60)
    print("🧪 Test 4: 拒绝非法文件类型")
    print("=" * 60)

    res = client.post(
        "/api/v1/jobs",
        files={"file": ("bad_file.txt", b"hello world", "text/plain")},
        data={"output_mode": "simplified"},
    )
    assert res.status_code == 400, f"Expected 400, got {res.status_code}"
    print(f"  Correctly rejected: {res.json().get('detail')}")
    print("  ✅ PASS")
    return True


def test_device_params():
    print("\n" + "=" * 60)
    print("🧪 Test 5: device 参数正确传递（kindle/apple）")
    print("=" * 60)

    if not Path(TEST_EPUB).exists():
        print("  ⏭️ SKIP")
        return True

    for device in ["kindle", "apple"]:
        with open(TEST_EPUB, "rb") as f:
            res = client.post(
                "/api/v1/jobs",
                files={"file": (f"test_{device}.epub", f, "application/epub+zip")},
                data={"output_mode": "simplified", "device": device},
            )
        data = res.json()
        assert data["device"] == device, f"Expected device={device}, got {data.get('device')}"

        result = wait_for_job(data["job_id"])
        assert result["status"] == "success", f"{device} job failed: {result.get('message')}"
        assert result["device"] == device
        print(f"  {device}: ✅ success")

    print("  ✅ PASS")
    return True


def test_job_not_found():
    print("\n" + "=" * 60)
    print("🧪 Test 6: 查询不存在的 job → 404")
    print("=" * 60)

    res = client.get("/api/v1/jobs/nonexistent_id")
    assert res.status_code == 404
    print("  ✅ PASS")
    return True


if __name__ == "__main__":
    passed = 0
    failed = 0

    tests = [
        test_healthz,
        test_upload_and_complete,
        test_download_valid_epub,
        test_reject_invalid_file,
        test_device_params,
        test_job_not_found,
    ]

    for test_fn in tests:
        try:
            if test_fn():
                passed += 1
        except Exception as e:
            print(f"  ❌ FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"📊 A2 E2E Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
