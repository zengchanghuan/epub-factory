"""Offline reconciliation: only verified, amount-matched receipts notify owner."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import os
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.models import Job, JobStatus, OutputMode
from app.storage import JobStore
from app.tasks import reconcile


class PaymentReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.store = JobStore()
        self.stack.enter_context(patch.object(reconcile, "job_store", self.store))
        self.query = self.stack.enter_context(patch.object(reconcile, "query_verified_trade"))
        self.event = self.stack.enter_context(patch.object(reconcile, "record_event"))
        self.email = Mock()
        self.dispatch = Mock()
        service = ModuleType("app.domain.payment_email_service")
        service.queue_paid_order_email = self.email
        pipeline = ModuleType("app.tasks.job_pipeline")
        pipeline.run_conversion = SimpleNamespace(delay=self.dispatch)
        self.stack.enter_context(patch.dict("sys.modules", {
            "app.domain.payment_email_service": service,
            "app.tasks.job_pipeline": pipeline,
        }))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch.dict(os.environ, {"TRANSLATION_PRICE_CNY": "5.99"}))

    def add(self, key="order-a", **kwargs):
        values = dict(id=key, source_filename="private.epub", input_path="/unused/private.epub",
                      output_mode=OutputMode.simplified, trace_id="offline-reconcile",
                      expected_amount="1.99", status=JobStatus.pending_payment,
                      created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        values.update(kwargs)
        job = Job(**values)
        self.store.add(job)
        return job

    def paid(self, number="order-a", amount="1.99", status="TRADE_SUCCESS"):
        self.query.return_value = dict(out_trade_no=number, total_amount=amount, trade_status=status)

    def run_task(self):
        return reconcile.reconcile_payments.run()

    def test_verified_single_receipt_notifies_and_releases_once(self):
        job = self.add()
        self.paid()
        result = self.run_task()
        self.assertEqual(result, {"checked": 1, "paid": 1, "closed": 0, "skipped": 0})
        self.assertEqual(job.status, JobStatus.pending)
        self.query.assert_called_once_with(job.id)
        self.email.assert_called_once_with(job.id, "1.99", "conversion", file_count=1, is_test_order=False)
        self.event.assert_called_once_with(self.store, job.id, "payment_succeeded", "verified_query")
        self.dispatch.assert_called_once_with(job.id, "")
        self.assertEqual(self.run_task()["checked"], 0)
        self.assertEqual(self.email.call_count, 1)

    def test_translation_uses_frozen_price_and_kind(self):
        self.add(enable_translation=True, expected_amount="38.98")
        self.paid(amount="38.98", status="TRADE_FINISHED")
        self.assertEqual(self.run_task()["paid"], 1)
        self.email.assert_called_once_with("order-a", "38.98", "translation", file_count=1, is_test_order=False)

    def test_batch_is_one_receipt_at_batch_total(self):
        self.add("part-a", batch_id="bundle", batch_index=0, batch_size=2, expected_amount="3.98")
        self.add("part-b", batch_id="bundle", batch_index=1, batch_size=2, expected_amount="")
        self.paid("batch_bundle", "3.98")
        self.assertEqual(self.run_task()["paid"], 1)
        self.query.assert_called_once_with("batch_bundle")
        self.email.assert_called_once_with("batch_bundle", "3.98", "batch", file_count=2, is_test_order=False)
        self.assertEqual(self.dispatch.call_count, 2)

    def test_batch_missing_frozen_price_cannot_fall_back(self):
        job = self.add(batch_id="bundle", expected_amount="")
        self.paid("batch_bundle", "5.99")
        self.assertEqual(self.run_task()["skipped"], 1)
        self.assertEqual(job.status, JobStatus.pending_payment)
        self.email.assert_not_called()
        self.dispatch.assert_not_called()

    def test_legacy_single_uses_historical_price(self):
        self.add(expected_amount="")
        self.paid(amount="5.99")
        self.assertEqual(self.run_task()["paid"], 1)
        self.email.assert_called_once_with("order-a", "5.99", "conversion", file_count=1, is_test_order=False)

    def test_mismatched_unknown_or_invalid_amount_never_releases(self):
        job = self.add()
        for amount in (None, "", "1.98", "0", "-1.99", "NaN", "Infinity", "bad"):
            with self.subTest(amount=amount):
                self.paid(amount=amount)
                self.assertEqual(self.run_task()["skipped"], 1)
                self.assertEqual(job.status, JobStatus.pending_payment)
        self.email.assert_not_called()
        self.event.assert_not_called()
        self.dispatch.assert_not_called()

    def test_unverified_or_wrong_order_response_does_nothing(self):
        job = self.add()
        for response in (None, {}, {"trade_status": "TRADE_SUCCESS", "total_amount": "1.99"},
                         {"out_trade_no": "unrelated", "trade_status": "TRADE_SUCCESS", "total_amount": "1.99"}):
            with self.subTest(response=response):
                self.query.return_value = response
                self.assertEqual(self.run_task()["skipped"], 1)
        self.assertEqual(job.status, JobStatus.pending_payment)
        self.email.assert_not_called()
        self.dispatch.assert_not_called()

    def test_email_queue_failure_does_not_block_paid_processing(self):
        job = self.add()
        self.paid()
        self.email.side_effect = RuntimeError("smtp secret must never reach logs")
        with self.assertLogs(reconcile.logger, "WARNING") as logged:
            self.assertEqual(self.run_task()["paid"], 1)
        self.assertNotIn("smtp secret", " ".join(logged.output))
        self.assertEqual(job.status, JobStatus.pending)
        self.dispatch.assert_called_once()

    def test_test_marker_reaches_service_for_suppression(self):
        self.add(is_test_order=True, expected_amount="0.01")
        self.paid(amount="0.01")
        self.assertEqual(self.run_task()["paid"], 1)
        self.email.assert_called_once_with("order-a", "0.01", "conversion", file_count=1, is_test_order=True)

    def test_existing_terminal_or_running_orders_are_not_scanned(self):
        for state in (JobStatus.success, JobStatus.failed, JobStatus.running, JobStatus.pending, JobStatus.cancelled):
            self.add(key=state.value, status=state)
        self.assertEqual(self.run_task()["checked"], 0)
        self.query.assert_not_called()
        self.email.assert_not_called()

    def test_unpaid_and_closed_orders_never_notify(self):
        job = self.add()
        self.paid(status="WAIT_BUYER_PAY")
        self.assertEqual(self.run_task()["skipped"], 1)
        self.paid(status="TRADE_CLOSED")
        self.assertEqual(self.run_task()["closed"], 1)
        self.assertEqual(job.status, JobStatus.cancelled)
        self.email.assert_not_called()
        self.dispatch.assert_not_called()

    def test_expired_unpaid_order_can_close_without_receipt(self):
        job = self.add(created_at=datetime.now(timezone.utc) - timedelta(hours=3))
        self.paid(status="WAIT_BUYER_PAY")
        self.assertEqual(self.run_task()["closed"], 1)
        self.assertEqual(job.status, JobStatus.cancelled)
        self.email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
