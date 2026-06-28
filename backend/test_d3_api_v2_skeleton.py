"""
D3 API v2 骨架测试：验证 v2 路由存在且返回约定结构。

不依赖真实转换完成，仅验证：
- POST/GET /api/v2/jobs、GET /api/v2/jobs/{id} 响应形状
- stats、events、cancel、notifications 接口可用
"""

import os
import sys
import unittest
import uuid
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import main as main_module
from fastapi.testclient import TestClient
from app.main import _job_can_download, _job_to_v2_detail, app
from app.models import ChunkStatus, ErrorCode, Job, JobChunk, JobStatus, OutputMode
from app.storage import job_store

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
        created = res.json()
        job_id = created["job_id"]

        res = self.client.get(f"/api/v2/jobs/{job_id}", params={"token": created["access_token"]})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["job_id"], job_id)
        self.assertIn("status", data)
        self.assertIn(data["status"], ("queued", "running", "completed", "qa_failed", "failed", "partial_completed", "cancelled"))
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

    def test_v2_partial_translation_has_no_download_url(self):
        """部分翻译失败即使有 output_path，也不应暴露下载链接。"""
        job = Job(
            id="d3_partial_download_guard",
            trace_id="trace",
            source_filename="partial.epub",
            input_path="/tmp/partial.epub",
            output_mode=OutputMode.simplified,
            status=JobStatus.success,
            output_path="/tmp/partial_out.epub",
            enable_translation=True,
            error_code=ErrorCode.PARTIAL_TRANSLATION.value,
            message="转换成功，但有 546 个段落翻译失败，成功写入 1868 个段落。",
        )
        detail = _job_to_v2_detail(job, f"/api/v2/jobs/{job.id}/download")
        self.assertFalse(_job_can_download(job))
        self.assertEqual(detail["status"], "qa_failed")
        self.assertIsNone(detail["download_url"])
        self.assertEqual(detail["qa_report"]["status"], "failed")
        self.assertTrue(detail["qa_report"]["retryable"])

    def test_v2_failed_partial_translation_is_qa_failed(self):
        """交付门槛失败没有产物时，也应展示为可重译的质检失败。"""
        job = Job(
            id=f"d3_failed_partial_{uuid.uuid4().hex[:8]}",
            trace_id="trace_failed_partial",
            source_filename="partial_failed.epub",
            input_path="/tmp/partial_failed.epub",
            output_mode=OutputMode.simplified,
            status=JobStatus.failed,
            enable_translation=True,
            error_code=ErrorCode.PARTIAL_TRANSLATION.value,
            message="AI 翻译失败：仍有 98/2414 个段落未成功翻译。",
            translation_stats={
                "total_chunks": 2414,
                "translated_chunks": 2300,
                "cached_chunks": 16,
                "failed_chunks": 98,
                "delivery_gate_failed": True,
            },
        )
        detail = _job_to_v2_detail(job, f"/api/v2/jobs/{job.id}/download")

        self.assertFalse(_job_can_download(job))
        self.assertEqual(detail["status"], "qa_failed")
        self.assertIsNone(detail["download_url"])
        self.assertEqual(detail["qa_report"]["status"], "failed")
        self.assertTrue(detail["qa_report"]["retryable"])

    def test_v2_retry_translation_reuses_original_job_without_payment(self):
        """质检失败后可复用原上传文件和 token 免费重译。"""
        suffix = uuid.uuid4().hex[:8]
        tmp_input = Path(f"/tmp/d3_retry_translation_source_{suffix}.epub")
        tmp_input.write_bytes(MINIMAL_EPUB_BYTES)
        job = Job(
            id=f"d3_retry_translation_{suffix}",
            trace_id="trace_retry",
            source_filename="retry.epub",
            input_path=str(tmp_input),
            access_token="retry-token",
            output_mode=OutputMode.simplified,
            status=JobStatus.success,
            output_path="/tmp/retry_out.epub",
            enable_translation=True,
            error_code=ErrorCode.PARTIAL_TRANSLATION.value,
            message="翻译未完成：有 2 个段落翻译失败",
            translation_stats={
                "total_chunks": 10,
                "failed_chunks": 2,
                "free_retry_count": 0,
                "translation_attempt": 1,
            },
        )
        job_store.add(job)

        with patch.object(main_module, "_enqueue_conversion") as enqueue:
            res = self.client.post(
                f"/api/v2/jobs/{job.id}/retry-translation",
                headers={"X-Job-Token": "retry-token"},
            )

        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["status"], "queued")
        self.assertIsNone(data["download_url"])
        self.assertEqual(data["translation_stats"]["free_retry_count"], 1)
        self.assertEqual(data["translation_stats"]["translation_attempt"], 2)
        self.assertEqual(data["qa_report"]["status"], "retrying")
        enqueue.assert_called_once()

    def test_v2_retry_translation_accepts_failed_delivery_gate_job(self):
        """超过交付门槛被拦截的失败任务，也能免费重译。"""
        suffix = uuid.uuid4().hex[:8]
        tmp_input = Path(f"/tmp/d3_retry_failed_delivery_gate_{suffix}.epub")
        tmp_input.write_bytes(MINIMAL_EPUB_BYTES)
        job = Job(
            id=f"d3_retry_failed_delivery_gate_{suffix}",
            trace_id="trace_retry_failed_delivery_gate",
            source_filename="retry_failed.epub",
            input_path=str(tmp_input),
            access_token="retry-token",
            output_mode=OutputMode.simplified,
            status=JobStatus.failed,
            enable_translation=True,
            error_code=ErrorCode.PARTIAL_TRANSLATION.value,
            message="AI 翻译失败：仍有 98/2414 个段落未成功翻译。",
            translation_stats={
                "total_chunks": 2414,
                "failed_chunks": 98,
                "free_retry_count": 0,
                "translation_attempt": 1,
                "delivery_gate_failed": True,
            },
        )
        job_store.add(job)

        with patch.object(main_module, "_enqueue_conversion") as enqueue:
            res = self.client.post(
                f"/api/v2/jobs/{job.id}/retry-translation",
                headers={"X-Job-Token": "retry-token"},
            )

        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["status"], "queued")
        self.assertEqual(data["translation_stats"]["free_retry_count"], 1)
        self.assertEqual(data["translation_stats"]["translation_attempt"], 2)
        enqueue.assert_called_once()

    def test_v2_translation_diagnostics_exposes_failed_chunks(self):
        """翻译诊断接口返回失败类别、失败段落和重试次数。"""
        suffix = uuid.uuid4().hex[:8]
        job = Job(
            id=f"d3_translation_diag_{suffix}",
            trace_id="trace_diag",
            source_filename="diag.epub",
            input_path="/tmp/diag.epub",
            access_token="diag-token",
            output_mode=OutputMode.simplified,
            status=JobStatus.failed,
            enable_translation=True,
            error_code=ErrorCode.TRANSLATION_FAILED.value,
            message="AI 翻译失败",
            translation_stats={
                "model": "deepseek-v4-flash",
                "total_chunks": 1,
                "failed_chunks": 1,
                "timeout_errors": 2,
                "retry_attempts": 4,
                "last_error": "untranslated response; retry still untranslated",
                "audit_flags_count": {"likely_untranslated": 1},
            },
        )
        job_store.add(job)
        job_store.upsert_chunk(JobChunk(
            job_id=job.id,
            chapter_id="part001",
            chunk_id="part001_0001",
            sequence=1,
            locator="p:nth-of-type(1)",
            source_hash="abc",
            source_text="This account of DNA is unique in several ways.",
            translated_text="This account of DNA is unique in several ways.",
            audit_json={"risk_level": "fail", "flags": ["likely_untranslated", "translation_error"]},
            status=ChunkStatus.failed,
            cached=False,
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            retry_count=3,
            latency_ms=91000,
            error_message="untranslated response; retry still untranslated",
        ))

        res = self.client.get(
            f"/api/v2/jobs/{job.id}/translation-diagnostics",
            headers={"X-Job-Token": "diag-token"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["summary"]["failed_chunks"], 1)
        self.assertEqual(data["summary"]["timeout_errors"], 2)
        self.assertEqual(data["error_categories"]["untranslated_response"], 1)
        self.assertEqual(data["failed_chunks"][0]["retry_count"], 3)

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
        created = res.json()
        job_id = created["job_id"]

        res = self.client.get(f"/api/v2/jobs/{job_id}/stats", params={"token": created["access_token"]})
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
        created = res.json()
        job_id = created["job_id"]

        res = self.client.get(f"/api/v2/jobs/{job_id}/events", params={"token": created["access_token"]})
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
        created = res.json()
        job_id = created["job_id"]

        res = self.client.post(f"/api/v2/jobs/{job_id}/cancel", params={"token": created["access_token"]})
        self.assertIn(res.status_code, (200, 400), res.text)
        if res.status_code == 200:
            data = res.json()
            self.assertEqual(data.get("status"), "cancelled")

    def test_cancelled_job_status_cannot_be_overwritten_by_worker(self):
        """用户取消后，后台线程迟到的成功/失败状态不能覆盖 cancelled。"""
        job_id = f"d3_cancel_lock_{uuid.uuid4().hex[:12]}"
        job_store.add(Job(
            id=job_id,
            source_filename="cancel_lock.epub",
            output_mode=OutputMode.simplified,
            trace_id=uuid.uuid4().hex,
            input_path="/tmp/cancel_lock.epub",
            enable_translation=True,
            status=JobStatus.running,
        ))

        cancelled = job_store.update_status(job_id, JobStatus.cancelled, "用户已停止翻译")
        self.assertIsNotNone(cancelled)
        overwritten = job_store.update_status(job_id, JobStatus.success, "转换成功")

        self.assertIsNotNone(overwritten)
        self.assertEqual(overwritten.status, JobStatus.cancelled)
        self.assertEqual(overwritten.message, "用户已停止翻译")

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
