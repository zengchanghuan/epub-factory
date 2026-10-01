"""Opt-in R6 historical close/late-payment/reload/conversion/download gate.

Inherits the R5 real-book artifact protections and fault replay. One additional
case runs all three SHA-pinned originals through verified gateway-close and
late payment, using webhook, customer recovery and reconciliation respectively.
Gateway and broker are substitutes; conversion and EPUBCheck are real, no LLM.
"""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
import unittest
from unittest.mock import patch

import test_d43_dispatch_history as previous

navigation = previous.navigation


class PaymentLifecycleHistoryTests(previous.DispatchHistoryTests):
    def test_late_payment_after_gateway_close_delivers_original_books_on_all_three_paths(self):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, update
        from app import main, job_runner
        from app.tasks import reconcile
        from app.domain.job_dispatch_service import dispatch_pending
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub
        from app.models import JobStatus
        from app.storage_db import Base, JobRecord, PersistentJobStore

        for channel, book in zip(("webhook", "recover", "reconcile"), navigation.BOOKS):
            with self.subTest(book=book["key"], channel=channel), ExitStack() as isolated:
                root = self.root / ("r6-late-" + book["key"])
                uploads, outputs = root / "uploads", root / "outputs"
                uploads.mkdir(parents=True)
                outputs.mkdir()
                engine = create_engine("sqlite:///" + str(root / "orders.db"))
                isolated.callback(engine.dispose)
                Base.metadata.create_all(engine)
                store = PersistentJobStore(engine)
                isolated.enter_context(patch.dict(os.environ, {
                    "SKIP_PAYMENT_CHECK": "0", "ALIPAY_APP_ID": "", "ALIPAY_SELLER_ID": "",
                    "DEEPSEEK_API_KEY": "", "DASHSCOPE_API_KEY": "", "GEMINI_API_KEY": "",
                    "JOB_DISPATCH_ENABLED": "0",
                }))
                for module, name, value in (
                    (main, "job_store", store), (job_runner, "job_store", store), (reconcile, "job_store", store),
                    (main, "UPLOAD_DIR", uploads), (main, "OUTPUT_DIR", outputs), (job_runner, "OUTPUT_DIR", outputs),
                ):
                    isolated.enter_context(patch.object(module, name, value))
                isolated.enter_context(patch("app.infra.execution_lease.tempfile.gettempdir", return_value=str(root)))
                isolated.enter_context(patch.object(main, "_use_celery", return_value=True))
                isolated.enter_context(patch.object(main, "create_alipay_page_pay", return_value="https://offline.invalid/pay"))
                isolated.enter_context(patch("app.infra.alipay.create_alipay_precreate", return_value=None))
                isolated.enter_context(patch.object(main, "verify_alipay_notification", return_value=True))
                isolated.enter_context(patch("app.domain.payment_email_service.queue_paid_order_email"))
                isolated.enter_context(patch.object(job_runner, "notify_job_completed"))
                isolated.enter_context(patch.object(job_runner, "report_error"))
                executing = isolated.enter_context(patch.object(job_runner, "_run_job_locked", wraps=job_runner._run_job_locked))
                publisher = isolated.enter_context(patch("app.infra.job_dispatch_publisher.publish_conversion",
                                                         side_effect=ConnectionError("offline unavailable")))
                client = TestClient(main.app)
                isolated.callback(client.close)
                with (self.uploads / book["input"]).open("rb") as source:
                    response = client.post("/api/v2/jobs", files={
                        "file": (book["input"], source, "application/epub+zip"),
                    }, data={"output_mode": "simplified", "enable_translation": "false",
                             "enable_precision_polish": "false", "lexicon_domains_json": "[]",
                             "enable_proper_noun": "false"})
                self.assertEqual(response.status_code, 200, response.text)
                job = store.get(response.json()["job_id"])
                self.assertEqual(job.status, JobStatus.pending_payment)
                with store._Session() as session:
                    session.execute(update(JobRecord).where(JobRecord.id == job.id).values(
                        created_at=datetime.now(timezone.utc) - timedelta(hours=3)))
                    session.commit()
                trade = {"out_trade_no": job.id, "total_amount": job.expected_amount, "trade_status": "TRADE_SUCCESS"}
                with patch.object(reconcile, "query_verified_trade", side_effect=[
                    {**trade, "trade_status": "WAIT_BUYER_PAY"}, {**trade, "trade_status": "TRADE_CLOSED"},
                ]), patch.object(reconcile, "close_verified_trade", return_value={"out_trade_no": job.id}) as close:
                    self.assertEqual(reconcile.reconcile_payments.run()["closed"], 1)
                close.assert_called_once_with(job.id)
                self.assertEqual(store.get(job.id).error_code, "PAYMENT_EXPIRED")
                self.assertEqual(store.list_dispatches(job.id), [])
                publisher.assert_not_called()

                store = PersistentJobStore(engine)
                main.job_store = job_runner.job_store = reconcile.job_store = store
                headers = {"X-Job-Token": job.access_token}
                if channel == "webhook":
                    self.assertEqual(client.post("/api/v2/webhooks/alipay", data=trade).text, "success")
                elif channel == "recover":
                    with patch("app.infra.alipay.query_verified_trade", return_value=trade):
                        response = client.post(f"/api/v2/jobs/{job.id}/recover", headers=headers)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertTrue(response.json()["recovered"])
                else:
                    with patch.object(reconcile, "query_verified_trade", return_value=trade):
                        self.assertEqual(reconcile.reconcile_payments.run()["paid"], 1)
                refreshed = store.get(job.id)
                self.assertEqual(refreshed.status, JobStatus.pending)
                self.assertEqual(refreshed.payment_resolution["state"], "paid")
                self.assertIn("closed_at", refreshed.payment_resolution)
                publisher.assert_called_once_with(job.id, "")
                row = store.list_dispatches(job.id)[0]
                self.assertEqual(row["status"], "pending")
                store = PersistentJobStore(engine)
                main.job_store = job_runner.job_store = reconcile.job_store = store
                queue = []
                publisher.side_effect = lambda key, attempt: queue.append((key, attempt))
                self.assertEqual(dispatch_pending(store, main._publish_conversion, now=row["next_attempt_at"] + 1)["sent"], 1)
                # A duplicate receipt after recovery does not publish again.
                self.assertEqual(client.post("/api/v2/webhooks/alipay", data=trade).text, "success")
                self.assertEqual(queue, [(job.id, "")])
                with redirect_stdout(io.StringIO()):
                    job_runner.run_job(*queue[0])
                self.assertEqual(executing.call_count, 1)
                delivered = store.get(job.id)
                self.assertEqual(delivered.status, JobStatus.success, delivered.message)
                self.assertEqual(delivered.expected_amount, job.expected_amount)
                self.assertEqual(navigation.sha256(navigation.Path(delivered.input_path)), book["input_sha256"])
                self.assertTrue(validate_epub(delivered.output_path, EPUBCHECK_JAR).passed)
                snapshot = navigation.BookSnapshot(delivered.output_path, self.opencc)
                reference = self.runs[book["key"]][1]
                self.assertEqual(snapshot.images, reference.images)
                self.assertEqual(set(snapshot.docs), set(reference.docs))
                for name in reference.docs:
                    self.assertTrue(snapshot.docs[name]["text"] == reference.docs[name]["text"],
                                    f"Historical text changed: {book['key']} {name}")
                    self.assertLessEqual(reference.docs[name]["ids"], snapshot.docs[name]["ids"])
                self.assertEqual([(r["label"], r["target"], r["depth"]) for r in snapshot.toc],
                                 [(r["label"], r["target"], r["depth"]) for r in reference.toc])
                main.job_store = PersistentJobStore(engine)
                detail = client.get(f"/api/v2/jobs/{job.id}", headers=headers)
                self.assertEqual(detail.json()["status"], "completed")
                self.assertEqual(detail.json()["payment_resolution"]["state"], "paid")
                self.assertEqual(detail.headers["cache-control"], "no-store")
                result = client.get(detail.json()["download_url"], headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(hashlib.sha256(result.content).hexdigest(),
                                 navigation.sha256(navigation.Path(delivered.output_path)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
