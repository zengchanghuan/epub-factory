"""R6 close/payment races at real reconciliation and in-memory store boundaries.

Only verified gateway responses and queue/mail transports are substitutes.
The companion store suite covers real SQLite serialization and rollback.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine

with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv", return_value=False):
    import test_d35_payment_reconcile as existing
    from app.models import JobStatus


class PaymentCloseRaceTests(unittest.TestCase):
    setUp = existing.PaymentReconciliationTests.setUp
    add = existing.PaymentReconciliationTests.add
    paid = existing.PaymentReconciliationTests.paid
    run_task = existing.PaymentReconciliationTests.run_task

    def expired(self, key="order-a", **kwargs):
        return self.add(key, created_at=datetime.now(timezone.utc) - timedelta(hours=3), **kwargs)

    def trade(self, number="order-a", status="TRADE_SUCCESS", amount="1.99"):
        return {"out_trade_no": number, "trade_status": status, "total_amount": amount}

    def assert_unreleased(self, job):
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.pending_payment)
        self.assertEqual(self.store.list_dispatches(job.id), [])
        self.dispatch.assert_not_called()
        self.email.assert_not_called()

    def test_failed_close_and_still_unpaid_never_fakes_local_close(self):
        job = self.expired()
        self.query.return_value = self.trade(status="WAIT_BUYER_PAY")
        self.assertEqual(self.run_task(), {"checked": 1, "paid": 0, "closed": 0, "skipped": 1})
        self.assert_unreleased(job)
        self.close.assert_called_once_with(job.id)
        self.assertEqual(self.query.call_count, 2)

    def test_unknown_followup_after_unknown_close_is_retryable(self):
        job = self.expired()
        self.query.side_effect = [self.trade(status="WAIT_BUYER_PAY"), None]
        self.assertEqual(self.run_task()["skipped"], 1)
        self.assert_unreleased(job)

    def test_wrong_close_order_is_not_close_evidence(self):
        job = self.expired()
        self.query.return_value = self.trade(status="WAIT_BUYER_PAY")
        self.close.return_value = {"out_trade_no": "some-other-order"}
        self.assertEqual(self.run_task()["skipped"], 1)
        self.assert_unreleased(job)

    def test_payment_wins_failed_close_race(self):
        job = self.expired()
        self.query.side_effect = [self.trade(status="WAIT_BUYER_PAY"), self.trade()]
        self.assertEqual(self.run_task()["paid"], 1)
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.pending)
        self.assertEqual(job.payment_resolution["state"], "paid")
        self.dispatch.assert_called_once_with(job.id, "")
        self.assertEqual(self.store.list_dispatches(job.id)[0]["status"], "sent")

    def test_verified_payment_has_priority_over_close_acknowledgement(self):
        job = self.expired()
        self.close.return_value = {"out_trade_no": job.id}
        self.query.side_effect = [self.trade(status="WAIT_BUYER_PAY"), self.trade()]
        result = self.run_task()
        self.assertEqual((result["paid"], result["closed"]), (1, 0))
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.pending)
        self.dispatch.assert_called_once()

    def test_paid_amount_mismatch_is_not_converted_to_closed_or_fulfilled(self):
        job = self.expired()
        self.close.return_value = {"out_trade_no": job.id}
        self.query.side_effect = [self.trade(status="WAIT_BUYER_PAY"), self.trade(amount="0.01")]
        self.assertEqual(self.run_task()["skipped"], 1)
        self.assert_unreleased(job)

    def test_wrong_order_followup_does_not_release_or_close_without_evidence(self):
        job = self.expired()
        self.query.side_effect = [self.trade(status="WAIT_BUYER_PAY"), self.trade(number="other")]
        self.assertEqual(self.run_task()["skipped"], 1)
        self.assert_unreleased(job)

    def test_verified_closed_followup_can_confirm_failed_close_call(self):
        job = self.expired()
        self.query.side_effect = [self.trade(status="WAIT_BUYER_PAY"), self.trade(status="TRADE_CLOSED")]
        self.assertEqual(self.run_task()["closed"], 1)
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.cancelled)
        self.assertEqual(job.error_code, "PAYMENT_EXPIRED")
        self.assertEqual(job.payment_resolution["state"], "closed")
        self.dispatch.assert_not_called()

    def test_confirmed_close_survives_unknown_followup_without_claiming_paid(self):
        job = self.expired()
        self.close.return_value = {"out_trade_no": job.id}
        self.query.side_effect = [self.trade(status="WAIT_BUYER_PAY"), None]
        self.assertEqual(self.run_task()["closed"], 1)
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.cancelled)
        self.email.assert_not_called()
        self.dispatch.assert_not_called()

    def test_delayed_query_after_confirmed_close_restores_original_order(self):
        job = self.expired()
        self.query.return_value = self.trade(status="TRADE_CLOSED")
        self.assertEqual(self.run_task()["closed"], 1)
        job = self.store.get(job.id)
        closed_at = job.payment_resolution["closed_at"]
        self.query.return_value = self.trade()
        self.assertEqual(self.run_task()["paid"], 1)
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.pending)
        self.assertEqual(job.payment_resolution["closed_at"], closed_at)
        self.assertEqual(job.payment_resolution["source"], "verified_query")
        self.dispatch.assert_called_once()
        self.assertEqual(self.run_task()["checked"], 0)

    def test_legacy_local_timeout_is_scanned_and_fulfilled_after_verified_query(self):
        job = self.expired(status=JobStatus.cancelled, message="支付超时，订单已关闭")
        self.query.return_value = self.trade()
        self.assertEqual(self.run_task()["paid"], 1)
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.pending)
        self.dispatch.assert_called_once()
        self.close.assert_not_called()

    def test_payment_callback_between_query_and_local_close_cannot_be_cancelled(self):
        job = self.expired()
        def query(_):
            self.store.settle_verified_payment(job.id, amount="1.99", source="verified_webhook")
            return self.trade(status="TRADE_CLOSED")
        self.query.side_effect = query
        self.run_task()
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.pending)
        self.assertEqual(job.payment_resolution["state"], "paid")
        self.assertEqual(len(self.store.list_dispatches(job.id)), 1)

    def test_cancelled_batch_leader_does_not_hide_unpaid_siblings(self):
        leader = self.expired("leader", batch_id="bundle", batch_index=0,
                              status=JobStatus.cancelled, message="用户取消", expected_amount="3.98")
        child = self.expired("child", batch_id="bundle", batch_index=1, expected_amount="")
        self.query.return_value = self.trade("batch_bundle", amount="3.98")
        self.assertEqual(self.run_task()["paid"], 1)
        self.query.assert_called_once_with("batch_bundle")
        leader, child = self.store.get(leader.id), self.store.get(child.id)
        self.assertEqual(leader.status, JobStatus.cancelled)
        self.assertEqual(leader.payment_resolution["state"], "paid_review")
        self.assertEqual(child.status, JobStatus.pending)
        self.dispatch.assert_called_once_with("child", "")

    def test_expired_batch_restores_once_per_child_without_new_order(self):
        jobs = [self.expired("part-" + str(index), batch_id="bundle", batch_index=index,
                             expected_amount="3.98" if index == 0 else "") for index in range(2)]
        self.query.return_value = self.trade("batch_bundle", status="TRADE_CLOSED", amount="3.98")
        self.assertEqual(self.run_task()["closed"], 1)
        jobs = [self.store.get(job.id) for job in jobs]
        self.assertTrue(all(job.status == JobStatus.cancelled for job in jobs))
        self.query.return_value = self.trade("batch_bundle", amount="3.98")
        self.assertEqual(self.run_task()["paid"], 1)
        self.assertEqual(self.dispatch.call_count, 2)
        jobs = [self.store.get(job.id) for job in jobs]
        self.assertTrue(all(job.status == JobStatus.pending for job in jobs))
        self.assertEqual(self.run_task()["checked"], 0)
        self.assertEqual(self.dispatch.call_count, 2)

    def test_unknown_old_cancelled_orders_are_not_silently_scanned_or_started(self):
        self.expired(status=JobStatus.cancelled, message="用户取消")
        self.assertEqual(self.run_task()["checked"], 0)
        self.query.assert_not_called()
        self.close.assert_not_called()
        self.dispatch.assert_not_called()


class PersistentPaymentCloseRaceTests(unittest.TestCase):
    """Exercise actual SQLite timestamp reload at the reconciler boundary."""
    add = existing.PaymentReconciliationTests.add
    run_task = existing.PaymentReconciliationTests.run_task

    def setUp(self):
        existing.PaymentReconciliationTests.setUp(self)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="epub-r6-reconcile-")))
        self.stack.enter_context(patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(root / "isolated.db"),
            "CELERY_BROKER_URL": "", "REDIS_URL": "", "OWNER_PAYMENT_EMAIL_ENABLED": "0",
            "TRANSLATION_PRICE_CNY": "5.99",
        }, clear=True))
        self.stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        self.stack.enter_context(patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")))
        from app.storage_db import Base, PersistentJobStore
        from app.tasks import reconcile
        self.engine = create_engine("sqlite:///" + str(root / "orders.db"), connect_args={"check_same_thread": False})
        self.stack.callback(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.stack.enter_context(patch.object(reconcile, "job_store", self.store))
        self.add(created_at=datetime.now(timezone.utc) - timedelta(hours=3))
        self.assertIsNone(self.store.get("order-a").created_at.tzinfo, "fixture must cross real SQLite reload")

    @staticmethod
    def trade(status):
        return {"out_trade_no": "order-a", "trade_status": status, "total_amount": "1.99"}

    def test_sqlite_wait_then_verified_close_safely_expires(self):
        self.query.side_effect = [self.trade("WAIT_BUYER_PAY"), self.trade("TRADE_CLOSED")]
        self.close.return_value = {"out_trade_no": "order-a", "trade_no": "offline-close-proof"}
        self.assertEqual(self.run_task(), {"checked": 1, "paid": 0, "closed": 1, "skipped": 0})
        after = self.store.get("order-a")
        self.assertEqual(after.status, JobStatus.cancelled)
        self.assertEqual(after.error_code, "PAYMENT_EXPIRED")
        self.assertEqual(after.payment_resolution["state"], "closed")
        self.close.assert_called_once_with("order-a")
        self.assertEqual(self.store.list_dispatches(), [])
        self.dispatch.assert_not_called()

    def test_sqlite_wait_then_racing_payment_releases_and_dispatches(self):
        self.query.side_effect = [self.trade("WAIT_BUYER_PAY"), self.trade("TRADE_SUCCESS")]
        self.close.return_value = None
        self.assertEqual(self.run_task(), {"checked": 1, "paid": 1, "closed": 0, "skipped": 0})
        after = self.store.get("order-a")
        self.assertEqual(after.status, JobStatus.pending)
        self.assertEqual(after.payment_resolution["state"], "paid")
        self.assertIsNone(after.error_code)
        self.assertEqual(self.store.list_dispatches()[0]["status"], "sent")
        self.dispatch.assert_called_once_with("order-a", "")
        self.close.assert_called_once_with("order-a")

    def test_sqlite_unknown_close_and_followup_remain_pending_without_intent(self):
        self.query.side_effect = [self.trade("WAIT_BUYER_PAY"), None]
        self.close.return_value = None
        self.assertEqual(self.run_task(), {"checked": 1, "paid": 0, "closed": 0, "skipped": 1})
        self.assertEqual(self.store.get("order-a").status, JobStatus.pending_payment)
        self.assertEqual(self.store.get("order-a").payment_resolution, {})
        self.assertEqual(self.store.list_dispatches(), [])
        self.dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
