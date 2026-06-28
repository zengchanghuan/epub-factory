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
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import main as main_module
from fastapi.testclient import TestClient
from app.main import _job_can_download, _job_to_v2_detail, _job_translation_timing, app
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

    def test_v2_create_translation_accepts_model_choice(self):
        """AI 翻译任务可选择 DeepSeek flash/pro 模型，并持久化到 Job。"""
        old_skip = os.environ.get("SKIP_PAYMENT_CHECK")
        os.environ["SKIP_PAYMENT_CHECK"] = "1"
        try:
            res = self.client.post(
                "/api/v2/jobs",
                files={"file": ("model_choice.epub", MINIMAL_EPUB_BYTES, "application/epub+zip")},
                data={
                    "output_mode": "simplified",
                    "device": "generic",
                    "enable_translation": "true",
                    "translation_model": "deepseek-v4-pro",
                },
            )
        finally:
            if old_skip is None:
                os.environ.pop("SKIP_PAYMENT_CHECK", None)
            else:
                os.environ["SKIP_PAYMENT_CHECK"] = old_skip
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data.get("translation_model"), "deepseek-v4-pro")
        job = job_store.get(data["job_id"])
        self.assertIsNotNone(job)
        self.assertEqual(job.translation_model, "deepseek-v4-pro")

    def test_v2_create_translation_rejects_unknown_model(self):
        """后端拒绝 UI 外的模型名，避免客户端绕过模型护栏。"""
        res = self.client.post(
            "/api/v2/jobs",
            files={"file": ("bad_model.epub", MINIMAL_EPUB_BYTES, "application/epub+zip")},
            data={
                "output_mode": "simplified",
                "enable_translation": "true",
                "translation_model": "deepseek-reasoner-pro",
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("translation_model", res.text)

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

    def test_v2_translation_timing_attribution(self):
        """任务详情返回翻译耗时归因，便于判断瓶颈。"""
        suffix = uuid.uuid4().hex[:8]
        job = Job(
            id=f"d3_timing_{suffix}",
            trace_id="trace_timing",
            source_filename="timing.epub",
            input_path="/tmp/timing.epub",
            output_mode=OutputMode.simplified,
            status=JobStatus.failed,
            enable_translation=True,
            error_code=ErrorCode.PARTIAL_TRANSLATION.value,
            message="AI 翻译失败：仍有 1/4 个段落未成功翻译。",
            metrics_summary="""
────────────────────────────────────────────────────
⏱  Pipeline [fast-translation] — 总耗时 30000 ms
────────────────────────────────────────────────────
  ✅ Preprocess                    1000.0 ms
  ✅ Manifest                       500.0 ms
  ✅ Glossary                      2000.0 ms
  ✅ TranslateMap                 15000.0 ms
  ✅ ReducePackage                 1000.0 ms
  ✅ Total                        20000.0 ms
────────────────────────────────────────────────────
""",
            translation_stats={
                "total_chunks": 4,
                "translated_chunks": 3,
                "cached_chunks": 0,
                "failed_chunks": 1,
                "api_calls": 3,
                "retry_attempts": 2,
                "elapsed_seconds": 18.5,
                "total_tokens": 1000,
                "cost_usd": 0.01,
            },
        )
        job_store.add(job)
        job_store.upsert_chunk(JobChunk(
            job_id=job.id,
            chapter_id="c1",
            chunk_id="c1_001",
            sequence=1,
            locator="c1.xhtml#1",
            source_hash="h1",
            status=ChunkStatus.translated,
            latency_ms=1200,
        ))
        job_store.upsert_chunk(JobChunk(
            job_id=job.id,
            chapter_id="c1",
            chunk_id="c1_002",
            sequence=2,
            locator="c1.xhtml#2",
            source_hash="h2",
            status=ChunkStatus.failed,
            latency_ms=90000,
            retry_count=2,
            error_message="untranslated response; retry still invalid",
        ))

        timing = _job_translation_timing(job)

        self.assertIsNotNone(timing)
        self.assertEqual(timing["total_ms"], 20000)
        self.assertEqual(timing["api_calls"], 3)
        self.assertEqual(timing["failed_chunks"], 1)
        self.assertEqual(timing["failure_categories"][0]["code"], "untranslated_response")
        self.assertEqual(timing["bottleneck"]["primary"], "model_stability")
        self.assertGreater(timing["model_share"], 0.8)

    def test_v2_translation_timing_derives_live_chunk_data(self):
        """运行中任务缺少汇总 stats 时，从 job_chunks 推断耗时与失败归因，但不冒充 API 次数。"""
        suffix = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc)
        job = Job(
            id=f"d3_timing_live_{suffix}",
            trace_id="trace_timing_live",
            source_filename="timing_live.epub",
            input_path="/tmp/timing_live.epub",
            output_mode=OutputMode.simplified,
            status=JobStatus.running,
            enable_translation=True,
            message="正在翻译",
            created_at=now - timedelta(minutes=5),
            updated_at=now - timedelta(seconds=10),
            translation_stats={"restart_count": 2},
        )
        job_store.add(job)
        fixtures = [
            (ChunkStatus.translated, 12000, 0, 100, 40, None),
            (ChunkStatus.translated, 16000, 1, 120, 50, None),
            (ChunkStatus.cached, 0, 0, 0, 0, None),
            (ChunkStatus.failed, 42000, 2, 80, 20, "html tag mismatch; retry still invalid"),
        ]
        for idx, (status, latency, retries, prompt, completion, error) in enumerate(fixtures, start=1):
            job_store.upsert_chunk(JobChunk(
                job_id=job.id,
                chapter_id="c1",
                chunk_id=f"c1_{idx:03d}",
                sequence=idx,
                locator=f"c1.xhtml#{idx}",
                source_hash=f"h{idx}",
                status=status,
                latency_ms=latency,
                retry_count=retries,
                prompt_tokens=prompt,
                completion_tokens=completion,
                error_message=error,
            ))
        job_store.upsert_chunk(JobChunk(
            job_id=job.id,
            chapter_id="c1",
            chunk_id="c1_warn",
            sequence=99,
            locator="c1.xhtml#warn",
            source_hash="h_warn",
            status=ChunkStatus.translated,
            audit_json={"risk_level": "warn", "flags": ["glossary_review"]},
        ))

        timing = _job_translation_timing(job)

        self.assertIsNotNone(timing)
        self.assertGreaterEqual(timing["total_ms"], 299000)
        self.assertTrue(timing["model_stage_estimated"])
        self.assertFalse(timing["api_calls_estimated"])
        self.assertEqual(timing["api_calls_source"], "unavailable")
        self.assertEqual(timing["api_calls"], 0)
        self.assertEqual(timing["chunk_latency_samples"], 3)
        self.assertEqual(timing["total_chunks"], 5)
        self.assertEqual(timing["translated_chunks"], 3)
        self.assertEqual(timing["cached_chunks"], 1)
        self.assertEqual(timing["failed_chunks"], 1)
        self.assertEqual(timing["retry_attempts"], 3)
        self.assertEqual(timing["chunk_retry_total"], 3)
        self.assertEqual(timing["tokens"]["total"], 410)
        self.assertEqual(timing["failure_categories"][0]["code"], "html_tag_mismatch")
        self.assertNotIn("other", [item["code"] for item in timing["failure_categories"]])
        self.assertEqual(timing["bottleneck"]["primary"], "model_stability")

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

    def test_v2_restart_translation_accepts_cancelled_job(self):
        """用户主动停止后，可复用原上传文件重启翻译。"""
        suffix = uuid.uuid4().hex[:8]
        tmp_input = Path(f"/tmp/d3_restart_cancelled_{suffix}.epub")
        tmp_input.write_bytes(MINIMAL_EPUB_BYTES)
        job = Job(
            id=f"d3_restart_cancelled_{suffix}",
            trace_id="trace_restart_cancelled",
            source_filename="restart_cancelled.epub",
            input_path=str(tmp_input),
            access_token="restart-token",
            output_mode=OutputMode.simplified,
            status=JobStatus.cancelled,
            enable_translation=True,
            message="用户已停止翻译",
            translation_stats={
                "free_retry_count": 0,
                "translation_attempt": 1,
            },
        )
        job_store.add(job)

        with patch.object(main_module, "_enqueue_conversion") as enqueue:
            res = self.client.post(
                f"/api/v2/jobs/{job.id}/restart-translation",
                headers={"X-Job-Token": "restart-token"},
            )

        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["status"], "queued")
        self.assertEqual(data["message"], "重启翻译已排队（第 2 次尝试）")
        self.assertEqual(data["translation_stats"]["free_retry_count"], 1)
        self.assertEqual(data["translation_stats"]["translation_attempt"], 2)
        self.assertEqual(data["translation_stats"]["restart_count"], 1)
        self.assertEqual(job_store.get(job.id).status, JobStatus.pending)
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
