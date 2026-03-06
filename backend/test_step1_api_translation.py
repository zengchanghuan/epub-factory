"""
Step 1 测试：验证 FastAPI 翻译参数接入

测试场景：
1. 默认创建任务（不带翻译参数）→ enable_translation=false
2. 显式启用翻译 → enable_translation=true, target_lang=zh-CN
3. 传入自定义 target_lang → 验证正确透传
"""

import httpx
from pathlib import Path
import time
import sys

BASE_URL = "http://127.0.0.1:8001"
TEST_FILE = Path("/Users/zengchanghuan/Desktop/workspace/epub-factory/backend/test_en.epub")


def wait_for_server(max_retries=10):
    for i in range(max_retries):
        try:
            r = httpx.get(f"{BASE_URL}/healthz", timeout=3.0)
            if r.status_code == 200:
                print("✅ Server is up")
                return True
        except Exception:
            pass
        print(f"⏳ Waiting for server... ({i+1}/{max_retries})")
        time.sleep(2)
    print("❌ Server not reachable")
    return False


def poll_job(job_id: str, timeout: int = 300) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        r = httpx.get(f"{BASE_URL}/api/v1/jobs/{job_id}", timeout=10.0)
        data = r.json()
        status = data.get("status")
        if status in ("success", "failed"):
            return data
        time.sleep(3)
    return {"status": "timeout"}


def test_default_no_translation():
    """场景 1：默认不带翻译参数"""
    print("\n" + "=" * 60)
    print("🧪 Test 1: 默认创建任务（不带翻译参数）")
    print("=" * 60)

    with open(TEST_FILE, "rb") as f:
        r = httpx.post(
            f"{BASE_URL}/api/v1/jobs",
            files={"file": ("test.epub", f, "application/epub+zip")},
            data={"output_mode": "simplified"},
            timeout=30.0,
        )
    r.raise_for_status()
    job = r.json()
    print(f"  Response: {job}")

    assert job["enable_translation"] == False, f"Expected False, got {job['enable_translation']}"
    assert job["target_lang"] == "zh-CN", f"Expected zh-CN, got {job['target_lang']}"
    print("  ✅ PASS: enable_translation=False, target_lang=zh-CN (defaults correct)")

    result = poll_job(job["job_id"], timeout=60)
    assert result["status"] == "success", f"Expected success, got {result['status']}: {result.get('message')}"
    print(f"  ✅ PASS: Job completed successfully (no translation)")
    return True


def test_enable_translation():
    """场景 2：显式启用翻译"""
    print("\n" + "=" * 60)
    print("🧪 Test 2: 启用翻译 (enable_translation=true)")
    print("=" * 60)

    with open(TEST_FILE, "rb") as f:
        r = httpx.post(
            f"{BASE_URL}/api/v1/jobs",
            files={"file": ("test_en.epub", f, "application/epub+zip")},
            data={
                "output_mode": "simplified",
                "enable_translation": "true",
                "target_lang": "zh-CN",
            },
            timeout=30.0,
        )
    r.raise_for_status()
    job = r.json()
    print(f"  Response: {job}")

    assert job["enable_translation"] == True, f"Expected True, got {job['enable_translation']}"
    assert job["target_lang"] == "zh-CN", f"Expected zh-CN, got {job['target_lang']}"
    print("  ✅ PASS: enable_translation=True, target_lang=zh-CN (params correct)")

    print("  ⏳ Waiting for translation job to complete (may take a few minutes)...")
    result = poll_job(job["job_id"], timeout=600)
    assert result["status"] == "success", f"Expected success, got {result['status']}: {result.get('message')}"
    assert result.get("download_url") is not None, "Expected download_url to be set"
    print(f"  ✅ PASS: Translation job completed. Download: {result['download_url']}")
    return True


def test_custom_target_lang():
    """场景 3：自定义目标语言"""
    print("\n" + "=" * 60)
    print("🧪 Test 3: 自定义 target_lang=ja")
    print("=" * 60)

    with open(TEST_FILE, "rb") as f:
        r = httpx.post(
            f"{BASE_URL}/api/v1/jobs",
            files={"file": ("test.epub", f, "application/epub+zip")},
            data={
                "output_mode": "simplified",
                "enable_translation": "true",
                "target_lang": "ja",
            },
            timeout=30.0,
        )
    r.raise_for_status()
    job = r.json()
    print(f"  Response: {job}")

    assert job["enable_translation"] == True, f"Expected True, got {job['enable_translation']}"
    assert job["target_lang"] == "ja", f"Expected ja, got {job['target_lang']}"
    print("  ✅ PASS: target_lang=ja (custom language correctly passed)")
    # 不等完成，只验证参数透传
    return True


if __name__ == "__main__":
    if not TEST_FILE.exists():
        print(f"❌ Test file not found: {TEST_FILE}")
        print("   Run crop_epub.py first to create it.")
        sys.exit(1)

    if not wait_for_server():
        sys.exit(1)

    passed = 0
    failed = 0

    for test_fn in [test_default_no_translation, test_enable_translation, test_custom_target_lang]:
        try:
            if test_fn():
                passed += 1
        except Exception as e:
            print(f"  ❌ FAIL: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"📊 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
