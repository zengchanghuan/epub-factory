"""批量转换 API：多文件创建、统一支付、原子解锁、汇总与 ZIP 下载。"""

import os
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.models import JobStage, JobStatus, StageStatus
from app.storage import job_store


client = TestClient(app)


def _files(count=2):
    return [("files", (f"book-{uuid.uuid4().hex[:6]}.epub", b"test-epub", "application/epub+zip")) for _ in range(count)]


def _create_batch(*, skip_payment=True):
    old = os.environ.get("SKIP_PAYMENT_CHECK")
    if skip_payment:
        os.environ["SKIP_PAYMENT_CHECK"] = "1"
    else:
        os.environ.pop("SKIP_PAYMENT_CHECK", None)
    try:
        with patch("app.main._enqueue_batch") as enqueue:
            response = client.post(
                "/api/v2/batches",
                files=_files(),
                data={"output_mode": "simplified", "traditional_variant": "tw", "device": "apple"},
                headers={"X-Client-Session": f"batch-test-{uuid.uuid4().hex}"},
            )
        return response, enqueue
    finally:
        if old is None:
            os.environ.pop("SKIP_PAYMENT_CHECK", None)
        else:
            os.environ["SKIP_PAYMENT_CHECK"] = old


class BatchConversionTest(unittest.TestCase):
    def test_stage_records_do_not_collide_within_same_millisecond(self):
        from app.storage_db import _stage_to_record
        now = datetime.now(timezone.utc)
        stage = JobStage(
            job_id="batch-stage-test",
            stage_name="validating",
            status=StageStatus.completed,
            started_at=now,
            finished_at=now,
        )
        self.assertNotEqual(_stage_to_record(stage).id, _stage_to_record(stage).id)

    def test_batch_requires_at_least_two_files(self):
        response = client.post("/api/v2/batches", files=_files(1))
        self.assertEqual(response.status_code, 400)
        self.assertIn("至少需要选择 2 个文件", response.json()["detail"])

    def test_batch_creation_reuses_one_token_and_keeps_child_jobs(self):
        response, enqueue = _create_batch(skip_payment=True)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "queued")
        self.assertEqual(data["file_count"], 2)
        self.assertEqual(len(data["jobs"]), 2)
        enqueue.assert_called_once()

        jobs = job_store.list_jobs_by_batch_id(data["batch_id"])
        self.assertEqual([job.batch_index for job in jobs], [0, 1])
        self.assertTrue(all(job.batch_size == 2 for job in jobs))
        self.assertTrue(all(job.access_token == data["access_token"] for job in jobs))
        self.assertTrue(all(job.output_mode.value == "simplified" for job in jobs))
        self.assertTrue(all(job.traditional_variant == "tw" for job in jobs))
        self.assertTrue(all(job.device.value == "apple" for job in jobs))

        detail = client.get(
            f"/api/v2/batches/{data['batch_id']}",
            headers={"X-Job-Token": data["access_token"]},
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["counts"]["queued"], 2)

    def test_batch_payment_is_one_aggregate_order_and_unlocks_once(self):
        with patch("app.infra.alipay.create_alipay_precreate", return_value="alipay://batch-qr") as precreate:
            response, _ = _create_batch(skip_payment=False)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "pending_payment")
        self.assertEqual(data["qr_code"], "alipay://batch-qr")
        self.assertGreater(float(data["amount"]), 5.99)
        self.assertEqual(precreate.call_count, 1)
        self.assertEqual(precreate.call_args.args[0], f"batch_{data['batch_id']}")

        with patch("app.main._enqueue_conversion") as enqueue:
            from app.main import _release_batch
            self.assertTrue(_release_batch(data["batch_id"]))
            self.assertFalse(_release_batch(data["batch_id"]))
        self.assertEqual(enqueue.call_count, 2)
        jobs = job_store.list_jobs_by_batch_id(data["batch_id"])
        self.assertTrue(all(job.status == JobStatus.pending for job in jobs))

    def test_batch_webhook_routes_one_order_to_batch_release(self):
        with patch("app.infra.alipay.create_alipay_precreate", return_value="alipay://batch-qr"):
            response, _ = _create_batch(skip_payment=False)
        data = response.json()
        form = {
            "trade_status": "TRADE_SUCCESS",
            "out_trade_no": f"batch_{data['batch_id']}",
            "total_amount": data["amount"],
            "sign": "test-sign",
            "sign_type": "RSA2",
            "app_id": os.environ.get("ALIPAY_APP_ID", ""),
            "seller_id": os.environ.get("ALIPAY_SELLER_ID", ""),
        }
        with patch("app.main.verify_alipay_notification", return_value=True), patch(
            "app.main._release_batch", return_value=True
        ) as release:
            webhook = client.post("/api/v2/webhooks/alipay", data=form)
        self.assertEqual(webhook.status_code, 200)
        self.assertEqual(webhook.text, "success")
        release.assert_called_once_with(data["batch_id"])

    def test_batch_partial_success_can_download_zip(self):
        response, _ = _create_batch(skip_payment=True)
        data = response.json()
        jobs = job_store.list_jobs_by_batch_id(data["batch_id"])
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "converted.epub"
            output.write_bytes(b"converted")
            job_store.update_status(jobs[0].id, JobStatus.success, "转换成功", output_path=str(output))
            job_store.update_status(jobs[1].id, JobStatus.failed, "转换失败")

            detail = client.get(
                f"/api/v2/batches/{data['batch_id']}",
                headers={"X-Job-Token": data["access_token"]},
            )
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["status"], "partial_completed")
            self.assertEqual(detail.json()["progress_percent"], 100)

            download = client.get(
                f"/api/v2/batches/{data['batch_id']}/download",
                headers={"X-Job-Token": data["access_token"]},
            )
            self.assertEqual(download.status_code, 200)
            archive_path = tmp_path / "batch.zip"
            archive_path.write_bytes(download.content)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.namelist(), ["converted.epub"])


if __name__ == "__main__":
    unittest.main()
