import httpx
from pathlib import Path
import time
import sys

def test_api():
    base_url = "http://127.0.0.1:8001"
    
    # 检查健康状态
    try:
        r = httpx.get(f"{base_url}/healthz")
        r.raise_for_status()
        print("✅ Server is up.")
    except Exception as e:
        print(f"❌ Server not available: {e}")
        sys.exit(1)

    workspace_dir = Path("/Users/zengchanghuan/Desktop/workspace/epub-factory")
    test_file = workspace_dir / "別把你的錢留到死：懂得花錢，是最好的投資——理想人生的9大財務思維.epub"
    
    if not test_file.exists():
        print(f"❌ Test file not found: {test_file}")
        sys.exit(1)

    print("--- Testing Job Creation ---")
    with open(test_file, "rb") as f:
        files = {"file": ("test.epub", f, "application/epub+zip")}
        data = {"output_mode": "simplified"}
        r = httpx.post(f"{base_url}/api/v1/jobs", files=files, data=data, timeout=30.0)
        r.raise_for_status()
        job = r.json()
        job_id = job.get("job_id")
        print(f"✅ Job created: {job_id}")

    print("--- Waiting for Job Completion ---")
    for _ in range(15):
        time.sleep(2)
        r = httpx.get(f"{base_url}/api/v1/jobs/{job_id}")
        r.raise_for_status()
        job_status = r.json().get("status")
        print(f"⏳ Job status: {job_status}")
        if job_status == "success":
            print("✅ Job succeeded! Output mode: simplified")
            break
        elif job_status == "failed":
            print(f"❌ Job failed: {r.json().get('message')}")
            sys.exit(1)
    else:
        print("❌ Job timed out")
        sys.exit(1)

if __name__ == "__main__":
    test_api()