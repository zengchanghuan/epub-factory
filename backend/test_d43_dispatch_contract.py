"""R5 actual HTTP/store contracts; only gateway and broker boundaries are fake.

No app startup is run, no real payment signature/gateway result is claimed, and
no book execution occurs. Temporary SQLite, private fixture files and hard DNS /
socket guards prevent touching production services, book caches or user files.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete

from test_epub_fixture import minimal_epub_bytes


class DispatchContractTests(unittest.TestCase):
    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epub-r5-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._patch(patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(self.root / "bootstrap.db"),
            "SKIP_PAYMENT_CHECK": "0", "ADMIN_SECRET": "", "EPUB_FAST_TRANSLATION": "1",
            "ALIPAY_APP_ID": "offline-app", "ALIPAY_SELLER_ID": "offline-seller",
            "CELERY_BROKER_URL": "", "REDIS_URL": "", "SENTRY_DSN": "",
            "REPAIR_UPLOAD_DIR": str(self.root / "repair"),
            "OWNER_PAYMENT_EMAIL_ENABLED": "0", "NOTIFY_EMAIL_ENABLED": "0",
            "EPUB_TRANSLATION_CHECKPOINT_DB": str(self.root / "checkpoints.db"),
            "EPUB_DEFAULT_TRANSLATION_MODEL": "deepseek-flash", "EPUB_TRANSLATION_MAX_FREE_RETRIES": "-1",
        }, clear=True))
        self._patch(patch("dotenv.load_dotenv", return_value=False))
        self.network = [self._patch(patch(target, side_effect=AssertionError("R5 forbids network")))
                        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo")]
        from app import main
        from app.models import Job, JobStatus, OutputMode
        from app.storage_db import Base, DispatchRecord, PersistentJobStore
        from app.domain.dispatch_intent import dispatch_identity
        from app.domain.job_dispatch_service import dispatch_pending
        from app.domain.payment_entitlement import quote_entitlement, grant_verified_entitlement
        self.main, self.Job, self.Status, self.OutputMode = main, Job, JobStatus, OutputMode
        self.DispatchRecord, self.Store = DispatchRecord, PersistentJobStore
        self.identity, self.drain = dispatch_identity, dispatch_pending
        self.quote, self.grant = quote_entitlement, grant_verified_entitlement
        self.engine = create_engine("sqlite:///" + str(self.root / "orders.db"), connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self._patch(patch.object(main, "job_store", self.store))
        self._patch(patch.object(main, "UPLOAD_DIR", self.root))
        self._patch(patch.object(main, "OUTPUT_DIR", self.root))
        self._patch(patch.object(main, "_use_celery", return_value=True))
        self.verify = self._patch(patch.object(main, "verify_alipay_notification", return_value=True))
        self.query = self._patch(patch("app.infra.alipay.query_verified_trade", return_value=None))
        self.publish = self._patch(patch("app.infra.job_dispatch_publisher.publish_conversion"))
        self.worker = self._patch(patch.object(main, "run_job", side_effect=AssertionError("R5 never executes book")))
        self.source = self.root / "fixture.epub"
        self.source.write_bytes(minimal_epub_bytes())
        api = FastAPI()
        api.add_api_route("/webhook", main.alipay_webhook, methods=["POST"])
        api.add_api_route("/jobs/{job_id}/recover", main.recover_job_payment, methods=["POST"])
        api.add_api_route("/jobs/{job_id}/restart", main.restart_translation_v2, methods=["POST"])
        api.add_api_route("/jobs/{job_id}/cancel", main.cancel_job_v2, methods=["POST"])
        api.add_api_route("/batches/{batch_id}/recover", main.recover_batch_payment_v2, methods=["POST"])
        self.client = TestClient(api)
        self.addCleanup(self.client.close)

    def tearDown(self):
        for network in self.network:
            network.assert_not_called()
        self.worker.assert_not_called()

    def job(self, key="book", **values):
        fields = dict(id=key, source_filename="fixture.epub", input_path=str(self.source), trace_id="offline",
                      access_token="owner-only", status=self.Status.pending_payment, expected_amount="1.99",
                      output_mode=self.OutputMode.simplified)
        fields.update(values)
        job = self.Job(**fields)
        job.payment_entitlement = self.quote(job)
        self.store.add(job)
        return self.store.get(key)

    def callback(self, order="book", **overrides):
        fields = dict(out_trade_no=order, total_amount="1.99", trade_status="TRADE_SUCCESS",
                      app_id="offline-app", seller_id="offline-seller")
        fields.update(overrides)
        return self.client.post("/webhook", data=fields)

    def recover(self, key="book", *, batch=False, token="owner-only"):
        return self.client.post(("/batches/" if batch else "/jobs/") + key + "/recover",
                                headers={"X-Job-Token": token})

    def trade(self, key="book", amount="1.99", **overrides):
        return {"out_trade_no": key, "total_amount": amount, "trade_status": "TRADE_SUCCESS", **overrides}

    def legacy_pending(self, key="book", **values):
        job = self.job(key, status=self.Status.pending, **values)
        with self.store._Session() as session:
            session.execute(delete(self.DispatchRecord).where(self.DispatchRecord.job_id == key))
            session.commit()
        self.assertEqual(self.store.list_dispatches(key), [])
        return job

    def test_failed_publish_does_not_undo_payment_and_reloaded_relay_needs_no_gateway(self):
        self.job(enable_translation=True)
        self.publish.side_effect = TimeoutError("offline broker down")
        self.assertEqual(self.callback().text, "success")
        job = self.store.get("book")
        row = self.store.list_dispatches("book")[0]
        self.assertEqual(job.status, self.Status.pending)
        self.assertEqual(job.payment_entitlement["state"], "paid")
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.assertGreater(row["next_attempt_at"], row["updated_at"])
        self.assertNotIn("offline broker down", row["last_error"])
        self.publish.side_effect = None
        reloaded = self.Store(self.engine)
        result = self.drain(reloaded, self.publish, now=row["next_attempt_at"] + 1)
        self.assertEqual((result["sent"], result["published"]), (1, 1))
        self.assertEqual(reloaded.get_dispatch(row["dispatch_id"])["status"], "sent")
        self.query.assert_not_called()
        self.assertEqual(self.publish.call_count, 2)

    def test_duplicate_verified_callbacks_only_publish_once(self):
        job = self.job(enable_translation=True)
        for trade_status in ("TRADE_SUCCESS", "TRADE_SUCCESS", "TRADE_FINISHED"):
            self.assertEqual(self.callback(trade_status=trade_status).text, "success")
        current = self.store.get(job.id)
        self.publish.assert_called_once_with(job.id, current.translation_stats["attempt_id"])
        self.assertEqual(len(self.store.list_dispatches(job.id)), 1)
        self.query.assert_not_called()

    def test_invalid_signature_account_amount_and_unpaid_trade_create_no_intent(self):
        self.job(enable_translation=True)
        self.verify.return_value = False
        self.assertEqual(self.callback().text, "fail")
        self.verify.return_value = True
        for params in ({"app_id": "other"}, {"seller_id": "other"}, {"total_amount": "0.01"},
                       {"total_amount": "nan"}, {"trade_status": "WAIT_BUYER_PAY"},
                       {"trade_status": "TRADE_CLOSED"}):
            self.callback(**params)
        self.callback(order="unknown")
        self.assertEqual(self.store.list_dispatches(), [])
        self.assertEqual(self.store.get("book").status, self.Status.pending_payment)
        self.assertEqual(self.store.get("book").payment_entitlement["state"], "quoted")
        self.publish.assert_not_called()

    def test_recover_authorization_precedes_gateway_and_intent_creation(self):
        self.legacy_pending()
        self.query.return_value = self.trade()
        self.assertEqual(self.recover(token="wrong").status_code, 403)
        self.query.assert_not_called()
        self.publish.assert_not_called()
        self.assertEqual(self.store.list_dispatches(), [])

    def test_batch_partial_transport_failure_preserves_and_recovers_only_failed_child(self):
        for index in range(3):
            self.job(f"child-{index}", batch_id="group", batch_index=index, batch_size=3,
                     expected_amount="5.97" if index == 0 else "")
        def publish(key, _attempt):
            if key == "child-1":
                raise ConnectionError("broker child failure")
        self.publish.side_effect = publish
        self.assertEqual(self.callback(order="batch_group", total_amount="5.97").text, "success")
        rows = {row["job_id"]: row for row in self.store.list_dispatches()}
        self.assertEqual({key: row["status"] for key, row in rows.items()},
                         {"child-0": "sent", "child-1": "pending", "child-2": "sent"})
        self.assertEqual(self.publish.call_count, 3)
        self.assertTrue(all(job.status == self.Status.pending for job in self.store.list_jobs()))
        self.publish.reset_mock(side_effect=True)
        reloaded = self.Store(self.engine)
        result = self.drain(reloaded, self.publish, now=rows["child-1"]["next_attempt_at"] + 1)
        self.assertEqual(result["sent"], 1)
        self.publish.assert_called_once_with("child-1", "")
        self.assertTrue(all(row["status"] == "sent" for row in reloaded.list_dispatches()))
        self.query.assert_not_called()

    def test_paid_retry_publishes_new_attempt_and_obsoletes_old_unsent_intent(self):
        job = self.job(status=self.Status.pending, enable_translation=True,
                       translation_stats={"attempt_id": "old-attempt"})
        self.grant(self.store, job, "1.99", "verified_query")
        self.store.update_status("book", self.Status.failed)
        result = self.client.post("/jobs/book/restart", headers={"X-Job-Token": "owner-only"})
        self.assertEqual(result.status_code, 200, result.text)
        current = self.store.get("book")
        self.assertNotEqual(current.translation_stats["attempt_id"], "old-attempt")
        self.publish.assert_called_once_with("book", current.translation_stats["attempt_id"])
        self.assertEqual(self.store.get_dispatch(self.identity("book", "old-attempt"))["status"], "obsolete")
        self.assertEqual(self.store.get_dispatch(self.identity("book", current.translation_stats["attempt_id"]))["status"], "sent")

    def test_legacy_pending_requires_verified_matching_gateway_receipt(self):
        self.legacy_pending(enable_translation=True)
        for receipt in (None, self.trade("other"), self.trade(amount="0.01"),
                        self.trade(trade_status="WAIT_BUYER_PAY")):
            self.query.return_value = receipt
            result = self.recover()
            self.assertEqual(result.status_code, 200)
            self.assertFalse(result.json()["recovered"])
            self.assertEqual(self.store.list_dispatches(), [])
            self.publish.assert_not_called()
        self.query.return_value = self.trade()
        result = self.recover()
        self.assertTrue(result.json()["recovered"], result.text)
        self.assertEqual(self.store.list_dispatches()[0]["status"], "sent")
        self.assertEqual(self.store.get("book").payment_entitlement["state"], "paid")
        self.publish.assert_called_once()

    def test_legacy_single_recovery_reports_durable_restore_when_broker_is_down(self):
        self.legacy_pending(enable_translation=True)
        self.query.return_value = self.trade()
        self.publish.side_effect = TimeoutError()
        result = self.recover()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["recovered"], result.text)
        self.assertEqual(self.store.get("book").status, self.Status.pending)
        row = self.Store(self.engine).list_dispatches()[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.query.assert_called_once_with("book")
        self.publish.assert_called_once()
        self.assertFalse(self.recover().json()["recovered"])
        self.query.assert_called_once_with("book")
        self.publish.assert_called_once()

    def test_existing_pending_intent_recover_does_not_verify_gateway_again(self):
        self.job()
        self.publish.side_effect = TimeoutError()
        self.callback()
        row = self.store.list_dispatches()[0]
        self.publish.side_effect = None
        with patch("app.domain.dispatch_intent.time.time", return_value=row["next_attempt_at"] + 1):
            result = self.recover()
        self.assertTrue(result.json()["recovered"], result.text)
        self.query.assert_not_called()
        self.assertEqual(self.publish.call_count, 2)

    def test_old_attempt_intent_does_not_bypass_current_attempt_payment_recovery(self):
        self.job(status=self.Status.pending, enable_translation=True, translation_stats={"attempt_id": "old"})
        self.store.update_status("book", self.Status.pending, translation_stats={"attempt_id": "current"})
        self.assertIsNone(self.store.get_dispatch(self.identity("book", "current")))
        self.assertFalse(self.recover().json()["recovered"])
        self.query.assert_called_once_with("book")
        self.publish.assert_not_called()
        self.query.return_value = self.trade()
        self.assertTrue(self.recover().json()["recovered"])
        self.publish.assert_called_once_with("book", "current")
        self.assertEqual(self.store.get_dispatch(self.identity("book", "old"))["status"], "obsolete")

    def test_cancelled_and_terminal_jobs_never_publish_on_callback_or_recover(self):
        for status in (self.Status.cancelled, self.Status.success, self.Status.failed):
            key = status.value
            self.job(key, status=self.Status.pending)
            self.store.update_status(key, status)
            self.assertEqual(self.callback(key).text, "success")
            self.assertFalse(self.recover(key).json()["recovered"])
        self.publish.assert_not_called()
        self.query.assert_not_called()
        self.assertTrue(all(row["status"] == "obsolete" for row in self.store.list_dispatches()))

    def test_owner_cancel_after_broker_outage_prevents_later_retry_delivery(self):
        self.job(enable_translation=True)
        self.publish.side_effect = TimeoutError()
        self.assertEqual(self.callback().text, "success")
        row = self.store.list_dispatches()[0]
        result = self.client.post("/jobs/book/cancel", headers={"X-Job-Token": "owner-only"})
        self.assertEqual(result.status_code, 200, result.text)
        self.publish.reset_mock(side_effect=True)
        counts = self.drain(self.Store(self.engine), self.publish, now=row["next_attempt_at"] + 1)
        self.assertEqual(counts["obsolete"], 1)
        self.publish.assert_not_called()
        self.assertEqual(self.store.get("book").status, self.Status.cancelled)
        self.query.assert_not_called()

    def test_unpaid_restart_does_not_create_new_intent_or_publish(self):
        self.job(enable_translation=True, status=self.Status.failed)
        result = self.client.post("/jobs/book/restart", headers={"X-Job-Token": "owner-only"})
        self.assertEqual(result.status_code, 402, result.text)
        self.assertEqual(self.store.list_dispatches(), [])
        self.publish.assert_not_called()
        self.query.assert_not_called()

    def test_batch_legacy_pending_requires_matching_gateway_before_creation(self):
        for index in range(2):
            self.legacy_pending(f"legacy-{index}", batch_id="legacy", batch_index=index, batch_size=2,
                                expected_amount="3.98" if index == 0 else "")
        for receipt in (None, self.trade("wrong", "3.98"), self.trade("batch_legacy", "0.01")):
            self.query.return_value = receipt
            self.assertFalse(self.recover("legacy", batch=True).json()["recovered"])
            self.assertEqual(self.store.list_dispatches(), [])
        self.query.return_value = self.trade("batch_legacy", "3.98")
        result = self.recover("legacy", batch=True)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.publish.call_count, 2)
        self.assertTrue(all(row["status"] == "sent" for row in self.store.list_dispatches()))
        self.assertTrue(result.json()["recovered"], "Successfully recovered legacy batch must report recovery")

    def test_batch_existing_intents_recover_without_gateway_and_no_duplicates(self):
        for index in range(2):
            self.job(f"child-{index}", batch_id="batch", batch_index=index, batch_size=2,
                     expected_amount="3.98" if index == 0 else "")
        self.assertEqual(self.callback("batch_batch", total_amount="3.98").text, "success")
        self.assertEqual(self.publish.call_count, 2)
        self.recover("batch", batch=True)
        self.assertEqual(self.publish.call_count, 2)
        self.query.assert_not_called()

    def test_legacy_batch_recovery_reports_durable_restore_even_when_broker_is_down(self):
        for index in range(2):
            self.legacy_pending(f"legacy-{index}", batch_id="legacy", batch_index=index, batch_size=2,
                                expected_amount="3.98" if index == 0 else "")
        self.query.return_value = self.trade("batch_legacy", "3.98")
        self.publish.side_effect = ConnectionError("offline broker unavailable")
        result = self.recover("legacy", batch=True)
        self.assertTrue(result.json()["recovered"], result.text)
        rows = self.Store(self.engine).list_dispatches()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "pending" and row["attempts"] == 1 for row in rows))
        self.assertTrue(all(job.status == self.Status.pending for job in self.store.list_jobs()))
        self.query.assert_called_once_with("batch_legacy")
        self.assertEqual(self.publish.call_count, 2)
        # A duplicate recovery sees the durable intents and honors backoff. It
        # does not query again or announce another successful publication.
        duplicate = self.recover("legacy", batch=True)
        self.assertFalse(duplicate.json()["recovered"])
        self.query.assert_called_once()
        self.assertEqual(self.publish.call_count, 2)


if __name__ == "__main__":
    unittest.main()
