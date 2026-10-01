"""R1 payment entitlement regressions. All stores/files/network are isolated."""
import os
import socket
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text
from fastapi import FastAPI
from fastapi.testclient import TestClient

with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv", return_value=False):
    from app import main
from app.models import Job, JobStatus, OutputMode
from app.storage import JobStore
from app.storage_db import Base, PersistentJobStore, OrderEventRecord, _ensure_compatible_schema
from app.domain.payment_entitlement import (
    quote_entitlement, grant_verified_entitlement, recover_legacy_entitlement,
    restart_entitlement_reason, grant_test_entitlement,
)
from test_epub_fixture import minimal_epub_bytes


class PaymentEntitlementTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch.dict(os.environ, {"SKIP_PAYMENT_CHECK": "0", "ADMIN_SECRET": "", "EPUB_TRANSLATION_MAX_FREE_RETRIES": "-1"}))
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.source = self.root / "source.epub"
        self.source.write_bytes(minimal_epub_bytes())
        self.engine = create_engine(f"sqlite:///{self.root / 'orders.db'}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.stack.enter_context(patch.object(main, "job_store", self.store))
        self.stack.enter_context(patch.object(main, "UPLOAD_DIR", self.root))
        self.enqueue = self.stack.enter_context(patch.object(main, "_enqueue_conversion"))
        self.worker = self.stack.enter_context(patch.object(main, "process_job"))
        self.stack.enter_context(patch.object(main, "_use_celery", return_value=False))
        self.pay = self.stack.enter_context(patch.object(main, "create_alipay_page_pay", return_value="https://example.invalid/pay"))
        api = FastAPI()
        api.add_api_route("/jobs", main.create_job_v2, methods=["POST"])
        api.add_api_route("/jobs/{job_id}/cancel", main.cancel_job_v2, methods=["POST"])
        api.add_api_route("/jobs/{job_id}/restart", main.restart_translation_v2, methods=["POST"])
        api.add_api_route("/jobs/{job_id}/retry", main.retry_translation_v2, methods=["POST"])
        self.client = self.stack.enter_context(TestClient(api))

    def job(self, *, paid=False, quoted=True, **kwargs):
        values = dict(id="book", source_filename="source.epub", input_path=str(self.source), trace_id="offline",
                      output_mode=OutputMode.simplified, enable_translation=True, access_token="owner-token",
                      status=JobStatus.failed, expected_amount="3.99")
        values.update(kwargs)
        job = Job(**values)
        if quoted:
            job.payment_entitlement = quote_entitlement(job)
        self.store.add(job)
        if paid:
            grant_verified_entitlement(self.store, job, job.expected_amount, "verified_query")
        return self.store.get(job.id)

    def restart(self, query=""):
        return self.client.post("/jobs/book/restart" + query, headers={"X-Job-Token": "owner-token"})

    def evidence(self, source="verified_webhook"):
        with self.store._Session() as session:
            session.add(OrderEventRecord(order_no="book", event="payment_succeeded", source=source,
                                         occurred_at=datetime.now(timezone.utc)))
            session.commit()

    def test_cancel_unpaid_profile_cannot_restart_or_consume_retry(self):
        self.job(status=JobStatus.awaiting_confirmation)
        cancelled = self.client.post("/jobs/book/cancel", headers={"X-Job-Token": "owner-token"})
        self.assertEqual(cancelled.status_code, 200)
        result = self.restart("?translation_model=deepseek-v4-pro&translation_quality=literary")
        self.assertEqual(result.status_code, 402, result.text)
        self.assertEqual(self.store.get("book").status, JobStatus.cancelled)
        self.assertEqual(self.store.get("book").translation_stats.get("free_retry_count", 0), 0)
        self.enqueue.assert_not_called()

    def test_execution_status_is_never_payment_proof(self):
        for status in (JobStatus.success, JobStatus.failed, JobStatus.cancelled):
            with self.subTest(status=status):
                job = Job(id=status.value, source_filename="x", input_path=str(self.source), trace_id="x",
                          output_mode=OutputMode.simplified, enable_translation=True, status=status)
                self.assertEqual(restart_entitlement_reason(job), "payment_review_required")

    def test_paid_original_plan_can_restart(self):
        self.job(paid=True)
        result = self.restart()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.enqueue.call_count, 1)
        self.assertEqual(self.store.get("book").payment_entitlement["state"], "paid")

    def test_reject_upgrades_without_changing_history_or_output(self):
        old_output = self.root / "output.epub"
        old_output.write_bytes(minimal_epub_bytes())
        job = self.job(paid=True, status=JobStatus.success, output_path=str(old_output),
                       translation_stats={"translation_attempt": 1, "prompt_tokens": 123})
        before = self.store.get(job.id)
        for query in ("?translation_model=deepseek-v4-pro", "?translation_quality=high", "?translation_quality=literary"):
            result = self.restart(query)
            self.assertEqual(result.status_code, 409, result.text)
        after = self.store.get(job.id)
        self.assertEqual(after.output_path, before.output_path)
        self.assertEqual(after.translation_stats, before.translation_stats)
        self.assertEqual(after.payment_entitlement, before.payment_entitlement)
        self.enqueue.assert_not_called()

    def test_flash_aliases_are_the_same_entitlement(self):
        self.job(paid=True, translation_model="deepseek-v4-flash-vision-exp")
        for alias in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
            job = self.store.get("book")
            self.assertEqual(restart_entitlement_reason(job, translation_model=alias), "")
        self.assertEqual(self.restart("?translation_model=deepseek-flash").status_code, 200)

    def test_model_mutation_does_not_rewrite_original_entitlement(self):
        self.job(paid=True, translation_quality="high", translation_model="deepseek-v4-pro")
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE epub_jobs SET translation_quality='standard', translation_model='deepseek-flash'"))
        current = self.store.get("book")
        grant_verified_entitlement(self.store, current, "3.99", "verified_webhook")
        self.assertEqual(self.restart().status_code, 409)
        allowed = self.restart("?translation_model=deepseek-v4-pro&translation_quality=high")
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_legacy_receipt_migrates_once_without_changing_job(self):
        job = self.job(quoted=False, output_path=str(self.source), translation_stats={"prompt_tokens": 12})
        self.evidence()
        migrated = recover_legacy_entitlement(self.store, job)
        self.assertEqual(migrated["source"], "legacy_verified_event")
        after = self.store.get(job.id)
        self.assertEqual(after.status, job.status)
        self.assertEqual(after.output_path, job.output_path)
        self.assertEqual(after.translation_stats, job.translation_stats)
        self.assertEqual(after.updated_at, job.updated_at)
        self.assertEqual(self.restart().status_code, 200)

    def test_browser_event_and_test_marker_are_not_payment_proof(self):
        job = self.job(quoted=False, is_test_order=True)
        self.evidence("browser")
        self.assertEqual(recover_legacy_entitlement(self.store, job), {})
        self.assertEqual(self.restart().status_code, 409)
        self.enqueue.assert_not_called()

    def test_legacy_restarted_plan_needs_explicit_admin_evidence(self):
        job = self.job(quoted=False, translation_stats={"translation_attempt": 2, "free_retry_count": 1})
        self.evidence()
        self.assertEqual(recover_legacy_entitlement(self.store, job), {})
        self.assertEqual(grant_verified_entitlement(self.store, job, "3.99", "verified_query"), {})
        self.assertEqual(self.restart().status_code, 409)
        granted = grant_verified_entitlement(self.store, job, "3.99", "verified_admin_query", allow_legacy_plan=True)
        self.assertTrue(granted["legacy_plan_approved"])
        self.assertEqual(granted["source"], "verified_admin_query")
        self.assertEqual(self.restart().status_code, 200)

    def test_receipt_amount_and_source_must_be_trusted(self):
        job = self.job()
        for amount, source in (("0.01", "verified_query"), ("3.99", "browser")):
            with self.assertRaises(ValueError):
                grant_verified_entitlement(self.store, job, amount, source)
        self.assertEqual(self.store.get(job.id).payment_entitlement["state"], "quoted")

    def test_explicit_server_test_bypass_is_persisted(self):
        with patch.dict(os.environ, {"SKIP_PAYMENT_CHECK": "1"}):
            response = self.client.post("/jobs", files={"file": ("fixture.epub", minimal_epub_bytes(), "application/epub+zip")},
                                        data={"enable_translation": "true"})
        self.assertEqual(response.status_code, 200, response.text)
        created = self.store.get(response.json()["job_id"])
        self.assertTrue(created.is_test_order)
        self.assertEqual(created.payment_entitlement["source"], "server_test_bypass")
        self.assertEqual(restart_entitlement_reason(created), "")
        self.pay.assert_not_called()

    def test_server_test_authorization_can_promote_an_existing_quote(self):
        job = self.job(status=JobStatus.confirming)
        granted = grant_test_entitlement(self.store, job)
        self.assertEqual(granted["source"], "server_test_bypass")
        self.assertEqual(granted["state"], "test_authorized")
        self.assertTrue(self.store.get(job.id).is_test_order)
        self.assertEqual(restart_entitlement_reason(self.store.get(job.id)), "")

    def test_client_cannot_set_test_bypass_in_production_mode(self):
        with patch.object(main, "_estimate_translation_pricing", return_value={"price_cny": "3.99", "total_chars": 1}):
            response = self.client.post("/jobs", files={"file": ("fixture.epub", minimal_epub_bytes(), "application/epub+zip")},
                                        data={"enable_translation": "true", "is_test_order": "true",
                                              "SKIP_PAYMENT_CHECK": "1", "payment_entitlement": '{"state":"paid"}'})
        self.assertEqual(response.status_code, 200, response.text)
        created = self.store.get(response.json()["job_id"])
        self.assertFalse(created.is_test_order)
        self.assertEqual(created.status, JobStatus.pending_payment)
        self.assertEqual(created.payment_entitlement["state"], "quoted")
        self.worker.assert_not_called()

    def test_memory_store_and_persistent_store_both_enforce_entitlement(self):
        for store in (JobStore(), self.store):
            job = Job(id="direct", source_filename="x", input_path=str(self.source), trace_id="x",
                      output_mode=OutputMode.simplified, enable_translation=True, status=JobStatus.cancelled)
            store.add(job)
            _, reason = store.restart_translation_attempt("direct", attempt_id="new", action_label="retry",
                                                         max_free_retries=-1, started_at=datetime.now(timezone.utc))
            self.assertEqual(reason, "payment_review_required")

    def test_schema_upgrade_does_not_grant_legacy_jobs(self):
        self.job(quoted=False)
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE epub_jobs DROP COLUMN payment_entitlement_json"))
        _ensure_compatible_schema(self.engine)
        self.assertEqual(self.store.get("book").payment_entitlement, {})


if __name__ == "__main__":
    unittest.main()
