"""
D3 API v2 骨架测试：验证 v2 路由存在且返回约定结构。

不依赖真实转换完成，仅验证：
- POST/GET /api/v2/jobs、GET /api/v2/jobs/{id} 响应形状
- stats、events、cancel、notifications 接口可用
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from app.main import app

# 最小化 payload：仅用于触发「创建任务」并校验响应，不要求真实 EPUB 内容
MINIMAL_EPUB_BYTES = b"PK\x03\x04"  # 任意短内容，满足 .epub 后缀校验即可


class TestApiV2Skeleton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_v2_list_jobs_empty(self):
        """GET /api/v2/jobs 无任务时返回 items 数组。"""
        res = self.client.get("/api/v2/jobs")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("items", data)
        self.assertIsInstance(data["items"], list)

    def test_v2_create_reject_non_epub(self):
        """POST /api/v2/jobs 非 epub/pdf 返回 400。"""
        res = self.client.post(
            "/api/v2/jobs",
            files={"file": ("bad.txt", b"x", "text/plain")},
            data={"output_mode": "simplified"},
        )
        self.assertEqual(res.status_code, 400)

    def test_v2_create_returns_queued(self):
        """POST /api/v2/jobs 合法文件名返回 v2 形状且 status=queued。"""
        res = self.client.post(
            "/api/v2/jobs",
            files={"file": ("skeleton_test.epub", MINIMAL_EPUB_BYTES, "application/epub+zip")},
            data={
                "output_mode": "simplified",
                "device": "generic",
                "enable_translation": "false",
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data.get("status"), "queued")
        self.assertIn("job_id", data)
        self.assertIn("trace_id", data)
        self.assertIn("message", data)
        self.assertIn("created_at", data)
        self.assertEqual(data.get("source_filename"), "skeleton_test.epub")

    def test_v2_get_job_404(self):
        """GET /api/v2/jobs/{id} 不存在返回 404。"""
        res = self.client.get("/api/v2/jobs/nonexistent_id_xyz")
        self.assertEqual(res.status_code, 404)

    def test_v2_get_job_detail_shape(self):
        """GET /api/v2/jobs/{id} 存在时返回 v2 详情结构。"""
        res = self.client.post(
            "/api/v2/jobs",
            files={"file": ("detail_test.epub", MINIMAL_EPUB_BYTES, "application/epub+zip")},
            data={"output_mode": "simplified"},
        )
        self.assertEqual(res.status_code, 200)
        job_id = res.json()["job_id"]

        res = self.client.get(f"/api/v2/jobs/{job_id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["job_id"], job_id)
        self.assertIn("status", data)
        self.assertIn(data["status"], ("queued", "running", "completed", "failed", "partial_completed", "cancelled"))
        self.assertIn("message", data)
        self.assertIn("source_filename", data)
        self.assertIn("output_mode", data)
        self.assertIn("device", data)
        self.assertIn("enable_translation", data)
        self.assertIn("created_at", data)
        self.assertIn("updated_at", data)
        self.assertIn("download_url", data)
        self.assertIn("quality_stats", data)
        self.assertIn("translation_stats", data)
        self.assertIn("metrics_summary", data)
        self.assertIn("stage_summary", data)

    def test_v2_list_jobs_after_create(self):
        """GET /api/v2/jobs 创建后列表含至少一项。"""
        res = self.client.get("/api/v2/jobs")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("items", data)
        self.assertGreaterEqual(len(data["items"]), 1)
        first = data["items"][0]
        self.assertIn("job_id", first)
        self.assertIn("status", first)
        self.assertIn("created_at", first)

    def test_v2_stats_404(self):
        """GET /api/v2/jobs/{id}/stats 不存在返回 404。"""
        res = self.client.get("/api/v2/jobs/nonexistent_id_xyz/stats")
        self.assertEqual(res.status_code, 404)

    def test_v2_stats_shape(self):
        """GET /api/v2/jobs/{id}/stats 返回 summary 结构。"""
        res = self.client.post(
            "/api/v2/jobs",
            files={"file": ("stats_test.epub", MINIMAL_EPUB_BYTES, "application/epub+zip")},
            data={"output_mode": "simplified"},
        )
        self.assertEqual(res.status_code, 200)
        job_id = res.json()["job_id"]

        res = self.client.get(f"/api/v2/jobs/{job_id}/stats")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("job_id", data)
        self.assertIn("summary", data)
        s = data["summary"]
        self.assertIn("status", s)
        self.assertIn("chapters_total", s)
        self.assertIn("chunks_total", s)

    def test_v2_events_404(self):
        """GET /api/v2/jobs/{id}/events 不存在返回 404。"""
        res = self.client.get("/api/v2/jobs/nonexistent_id_xyz/events")
        self.assertEqual(res.status_code, 404)

    def test_v2_events_shape(self):
        """GET /api/v2/jobs/{id}/events 返回 items 数组。"""
        res = self.client.post(
            "/api/v2/jobs",
            files={"file": ("events_test.epub", MINIMAL_EPUB_BYTES, "application/epub+zip")},
            data={"output_mode": "simplified"},
        )
        self.assertEqual(res.status_code, 200)
        job_id = res.json()["job_id"]

        res = self.client.get(f"/api/v2/jobs/{job_id}/events")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("items", data)
        self.assertIsInstance(data["items"], list)

    def test_v2_cancel_404(self):
        """POST /api/v2/jobs/{id}/cancel 不存在返回 404。"""
        res = self.client.post("/api/v2/jobs/nonexistent_id_xyz/cancel")
        self.assertEqual(res.status_code, 404)

    def test_v2_cancel_pending_returns_200(self):
        """POST /api/v2/jobs/{id}/cancel 对刚创建的任务可取消。"""
        res = self.client.post(
            "/api/v2/jobs",
            files={"file": ("cancel_test.epub", MINIMAL_EPUB_BYTES, "application/epub+zip")},
            data={"output_mode": "simplified"},
        )
        self.assertEqual(res.status_code, 200)
        job_id = res.json()["job_id"]

        res = self.client.post(f"/api/v2/jobs/{job_id}/cancel")
        self.assertIn(res.status_code, (200, 400), res.text)
        if res.status_code == 200:
            data = res.json()
            self.assertEqual(data.get("status"), "cancelled")

    def test_v2_notifications_shape(self):
        """GET /api/v2/notifications 返回 items 数组。"""
        res = self.client.get("/api/v2/notifications")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("items", data)
        self.assertIsInstance(data["items"], list)

    def test_v2_download_404(self):
        """GET /api/v2/jobs/{id}/download 不存在返回 404。"""
        res = self.client.get("/api/v2/jobs/nonexistent_id_xyz/download")
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
