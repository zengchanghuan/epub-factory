"""R5 opt-in historical payment/dispatch/restart/delivery regression.

Three SHA-pinned original EPUBs take the real upload, persistent outbox and
ordinary conversion path, including real EPUBCheck. The payment verification
and broker are fault-injection substitutes; no model or external service runs.
Two accepted messages after an ambiguous publisher acknowledgement must execute
only one conversion. Existing original/delivered books remain read-only.
"""
from __future__ import annotations

import hashlib
import io
import os
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

import test_d40_reduce_history as replay

navigation = replay.tables.navigation


class DispatchHistoryTests(replay.ReduceHistoryTests):
    def test_actual_historical_delivery_after_broker_failure_ack_loss_and_store_reload(self):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from app import main, job_runner
        from app.domain.job_dispatch_service import dispatch_pending
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub
        from app.models import JobStatus
        from app.storage_db import Base, PersistentJobStore

        for book in navigation.BOOKS:
            with self.subTest(book=book["key"]), ExitStack() as isolated:
                root = self.root / ("r5-dispatch-" + book["key"])
                uploads, outputs = root / "uploads", root / "outputs"
                uploads.mkdir(parents=True)
                outputs.mkdir()
                engine = create_engine("sqlite:///" + str(root / "jobs.sqlite3"))
                isolated.callback(engine.dispose)
                Base.metadata.create_all(engine)
                store = PersistentJobStore(engine)
                isolated.enter_context(patch.dict(os.environ, {
                    "SKIP_PAYMENT_CHECK": "0", "ALIPAY_APP_ID": "", "ALIPAY_SELLER_ID": "",
                    "DEEPSEEK_API_KEY": "", "DASHSCOPE_API_KEY": "", "GEMINI_API_KEY": "",
                    "JOB_DISPATCH_ENABLED": "0",
                }))
                for module, name, value in (
                    (main, "job_store", store), (job_runner, "job_store", store),
                    (main, "UPLOAD_DIR", uploads), (main, "OUTPUT_DIR", outputs),
                    (job_runner, "OUTPUT_DIR", outputs),
                ):
                    isolated.enter_context(patch.object(module, name, value))
                isolated.enter_context(patch("app.infra.execution_lease.tempfile.gettempdir", return_value=str(root)))
                isolated.enter_context(patch.object(main, "_use_celery", return_value=True))
                isolated.enter_context(patch.object(main, "create_alipay_page_pay", return_value="https://offline.invalid/payment"))
                isolated.enter_context(patch("app.infra.alipay.create_alipay_precreate", return_value=None))
                isolated.enter_context(patch.object(main, "verify_alipay_notification", return_value=True))
                isolated.enter_context(patch("app.domain.payment_email_service.queue_paid_order_email"))
                isolated.enter_context(patch.object(job_runner, "notify_job_completed"))
                isolated.enter_context(patch.object(job_runner, "report_error"))
                executing = isolated.enter_context(patch.object(job_runner, "_run_job_locked", wraps=job_runner._run_job_locked))
                client = TestClient(main.app)
                isolated.callback(client.close)
                queue, available = [], False

                def publish(job_id, attempt_id):
                    if not available:
                        raise ConnectionError("offline broker failure")
                    queue.append((job_id, attempt_id))

                transport = isolated.enter_context(patch("app.infra.job_dispatch_publisher.publish_conversion", side_effect=publish))
                with (self.uploads / book["input"]).open("rb") as source:
                    response = client.post("/api/v2/jobs", files={
                        "file": (book["input"], source, "application/epub+zip"),
                    }, data={"output_mode": "simplified", "enable_translation": "false",
                             "enable_precision_polish": "false", "lexicon_domains_json": "[]",
                             "enable_proper_noun": "false"})
                self.assertEqual(response.status_code, 200, response.text)
                job = store.get(response.json()["job_id"])
                self.assertEqual(job.status, JobStatus.pending_payment)
                self.assertEqual(store.list_dispatches(job.id), [])
                original_upload = job.input_path
                headers = {"X-Job-Token": job.access_token}
                receipt = {"out_trade_no": job.id, "total_amount": job.expected_amount,
                           "trade_status": "TRADE_SUCCESS", "sign": "offline-verified-boundary"}
                self.assertEqual(client.post("/api/v2/webhooks/alipay", data=receipt).text, "success")
                first = store.list_dispatches(job.id)
                self.assertEqual(len(first), 1)
                self.assertEqual(first[0]["status"], "pending")
                self.assertEqual(first[0]["attempts"], 1)
                self.assertEqual(store.get(job.id).status, JobStatus.pending)
                self.assertFalse(queue)
                # A duplicate callback cannot skip the recorded backoff.
                self.assertEqual(client.post("/api/v2/webhooks/alipay", data=receipt).text, "success")
                self.assertEqual(transport.call_count, 1)

                store = PersistentJobStore(engine)
                main.job_store = job_runner.job_store = store
                available = True
                due = first[0]["next_attempt_at"] + 0.1
                with patch.object(store, "finish_dispatch", side_effect=RuntimeError("Injected post-publish acknowledgement loss")):
                    result = dispatch_pending(store, main._publish_conversion, job_id=job.id, now=due)
                self.assertEqual(result["published"], 1)
                self.assertEqual(result["sent"], 0)
                leased = store.get_dispatch(first[0]["dispatch_id"])
                self.assertEqual(leased["status"], "publishing")

                store = PersistentJobStore(engine)
                main.job_store = job_runner.job_store = store
                result = dispatch_pending(store, main._publish_conversion, job_id=job.id,
                                          now=leased["lease_expires_at"] + 0.1)
                self.assertEqual(result["sent"], 1)
                self.assertEqual(queue, [(job.id, ""), (job.id, "")])
                with redirect_stdout(io.StringIO()):
                    for job_id, attempt_id in queue:
                        job_runner.run_job(job_id, expected_attempt_id=attempt_id)
                self.assertEqual(executing.call_count, 1, "Two accepted messages executed the historical book twice")
                delivered = store.get(job.id)
                self.assertEqual(delivered.status, JobStatus.success, delivered.message)
                self.assertEqual(delivered.input_path, original_upload)
                self.assertEqual(delivered.expected_amount, job.expected_amount)
                self.assertEqual(navigation.sha256(navigation.Path(original_upload)), book["input_sha256"])
                self.assertTrue(validate_epub(delivered.output_path, EPUBCHECK_JAR).passed)
                snapshot = navigation.BookSnapshot(delivered.output_path, self.opencc)
                reference = self.runs[book["key"]][1]
                self.assertEqual(snapshot.images, reference.images)
                self.assertEqual(set(snapshot.docs), set(reference.docs))
                for name in reference.docs:
                    self.assertTrue(snapshot.docs[name]["text"] == reference.docs[name]["text"],
                                    f"Historical body/navigation text changed: {book['key']} {name}")
                    self.assertLessEqual(reference.docs[name]["ids"], snapshot.docs[name]["ids"])
                self.assertEqual([(row["label"], row["target"], row["depth"]) for row in snapshot.toc],
                                 [(row["label"], row["target"], row["depth"]) for row in reference.toc])
                # Reload again, then exercise the user's refresh/download path.
                main.job_store = PersistentJobStore(engine)
                detail = client.get(f"/api/v2/jobs/{job.id}", headers=headers)
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()["status"], "completed")
                self.assertEqual(detail.headers["cache-control"], "no-store")
                download = client.get(detail.json()["download_url"], headers=headers)
                self.assertEqual(download.status_code, 200)
                self.assertEqual(hashlib.sha256(download.content).hexdigest(),
                                 navigation.sha256(navigation.Path(delivered.output_path)))
                self.assertEqual(store.get_dispatch(first[0]["dispatch_id"])["status"], "sent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
