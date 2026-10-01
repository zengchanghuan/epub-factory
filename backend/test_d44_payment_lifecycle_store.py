"""R6 payment/close races with memory and temporary SQLite; no gateway IO."""
import os
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, text

with patch.dict(os.environ, {}, clear=True):
    from app.storage import JobStore
from app.models import ErrorCode, Job, JobStatus, OutputMode
from app.storage_db import Base, DispatchRecord, PersistentJobStore, _ensure_compatible_schema
from app.domain.dispatch_intent import build_dispatch_intent, timestamp
from app.domain.payment_lifecycle_state import is_payment_expired, PAYMENT_REVIEW_MESSAGE


class PaymentLifecycleStoreTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")))
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.engine = create_engine(f"sqlite:///{self.root / 'jobs.db'}", connect_args={"check_same_thread": False})
        self.stack.callback(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.memory, self.sql = JobStore(), PersistentJobStore(self.engine)
        self.stores = (self.memory, self.sql)

    def job(self, name="book", **values):
        fields = dict(id=name, source_filename="unused.epub", input_path="/never-opened/unused.epub",
                      trace_id="offline", output_mode=OutputMode.simplified, status=JobStatus.pending_payment,
                      created_at=datetime.now(timezone.utc) - timedelta(hours=2))
        fields.update(values)
        return Job(**fields)

    @staticmethod
    def settle(store, name="book", **values):
        return store.settle_verified_payment(name, source="verified_webhook", amount="1.99", **values)

    def assert_paid(self, store, key="book"):
        job = store.get(key)
        self.assertEqual(job.status, JobStatus.pending)
        self.assertIsNone(job.error_code)
        self.assertEqual(job.payment_resolution["state"], "paid")
        self.assertEqual(job.payment_resolution["source"], "verified_webhook")
        self.assertEqual(job.payment_resolution["amount"], "1.99")
        self.assertTrue(job.payment_resolution["verified_at"])
        self.assertEqual(len(store.list_dispatches(key)), 1)

    def test_pending_settlement_atomically_saves_resolution_and_attempt_dispatch(self):
        for store in self.stores:
            store.add(self.job(enable_translation=True, translation_stats={"kept": 4}))
            self.assertEqual(self.settle(store), {"released": ["book"], "review": [], "unchanged": []})
            self.assert_paid(store)
            current = store.get("book")
            self.assertEqual(current.translation_stats["kept"], 4)
            self.assertEqual(store.list_dispatches()[0]["attempt_id"], current.translation_stats["attempt_id"])

    def test_unconfirmed_close_is_noop_even_when_old(self):
        for store in self.stores:
            store.add(self.job())
            for value in (False, None, "true", 1):
                self.assertFalse(store.mark_payment_timeout("book", gateway_confirmed=value))
            self.assertFalse(store.mark_payment_timeout("book"))
            self.assertEqual(store.get("book").status, JobStatus.pending_payment)
            self.assertEqual(store.get("book").payment_resolution, {})
            self.assertEqual(store.list_dispatches(), [])

    def test_verified_close_then_late_payment_recovers_with_audit_history(self):
        for store in self.stores:
            store.add(self.job(enable_translation=True))
            self.assertTrue(store.mark_payment_timeout("book", gateway_confirmed=True))
            expired = store.get("book")
            self.assertTrue(is_payment_expired(expired))
            self.assertEqual(expired.error_code, ErrorCode.PAYMENT_EXPIRED.value)
            self.assertEqual(expired.payment_resolution["state"], "closed")
            closed_at = expired.payment_resolution["closed_at"]
            self.assertEqual(store.list_dispatches(), [])
            self.assertEqual(self.settle(store)["released"], ["book"])
            self.assert_paid(store)
            self.assertEqual(store.get("book").payment_resolution["closed_at"], closed_at)
            self.assertEqual(store.get("book").payment_resolution["closed_source"], "verified_gateway_close")

    def test_payment_then_verified_close_cannot_cancel_released_job(self):
        for store in self.stores:
            store.add(self.job())
            self.settle(store)
            before = store.get("book").payment_resolution.copy()
            self.assertFalse(store.mark_payment_timeout("book", gateway_confirmed=True))
            self.assert_paid(store)
            self.assertEqual(store.get("book").payment_resolution, before)

    def test_timeout_cannot_overwrite_any_nonwaiting_state(self):
        for store in self.stores:
            for status in JobStatus:
                if status == JobStatus.pending_payment:
                    continue
                store.add(self.job(status.value, status=status, message="preserve", error_code="KEEP"))
                self.assertFalse(store.mark_payment_timeout(status.value, gateway_confirmed=True))
                after = store.get(status.value)
                self.assertEqual((after.status, after.message, after.error_code), (status, "preserve", "KEEP"))
                self.assertEqual(after.payment_resolution, {})

    def test_user_cancelled_late_payment_is_review_only_and_repeat_is_unchanged(self):
        for store in self.stores:
            store.add(self.job(status=JobStatus.cancelled, message="用户已停止翻译", error_code="CANCELLED_BY_OWNER", enable_translation=True))
            self.assertEqual(self.settle(store), {"released": [], "review": ["book"], "unchanged": []})
            current = store.get("book")
            self.assertEqual(current.status, JobStatus.cancelled)
            self.assertEqual(current.message, PAYMENT_REVIEW_MESSAGE)
            self.assertEqual(current.error_code, ErrorCode.PAYMENT_REVIEW_REQUIRED.value)
            self.assertEqual(current.payment_resolution["state"], "paid_review")
            self.assertEqual(current.payment_resolution["original_cancel_message"], "用户已停止翻译")
            self.assertEqual(current.payment_resolution["original_error_code"], "CANCELLED_BY_OWNER")
            before = current.payment_resolution.copy()
            self.assertEqual(self.settle(store), {"released": [], "review": [], "unchanged": ["book"]})
            self.assertEqual(store.get("book").payment_resolution, before)
            self.assertEqual(store.list_dispatches(), [])

    def test_only_exact_legacy_messages_or_explicit_expiry_are_recoverable(self):
        cases = (
            ({"message": "支付超时，订单已关闭"}, True),
            ({"message": "支付超时，批次订单已关闭"}, True),
            ({"error_code": "PAYMENT_EXPIRED"}, True),
            ({"payment_resolution": {"state": "closed"}}, True),
            ({"message": "支付超时，订单已关闭 "}, False),
            ({"message": "订单已取消"}, False),
            ({"message": "用户取消：支付超时，订单已关闭"}, False),
            ({"message": "支付超时，订单已关闭", "payment_resolution": {"state": "paid_review"}}, False),
        )
        for store in self.stores:
            for index, (values, expired) in enumerate(cases):
                key = str(index)
                store.add(self.job(key, status=JobStatus.cancelled, **values))
                self.assertEqual(is_payment_expired(store.get(key)), expired)
                result = self.settle(store, key)
                if expired:
                    self.assertEqual(result["released"], [key])
                else:
                    self.assertEqual(result["released"], [])
                    self.assertEqual(store.get(key).status, JobStatus.cancelled)
                    self.assertEqual(store.list_dispatches(key), [])
            self.assertFalse(is_payment_expired(self.job(status=JobStatus.pending, error_code="PAYMENT_EXPIRED")))

    def test_duplicate_receipt_never_restarts_active_or_terminal_execution(self):
        for store in self.stores:
            for status in (JobStatus.pending, JobStatus.running, JobStatus.success, JobStatus.failed,
                           JobStatus.awaiting_confirmation, JobStatus.confirming):
                key = status.value
                store.add(self.job(key, status=status, message="unchanged", output_path="/preserve/output.epub",
                                   translation_stats={"attempt_id": "old", "prompt_tokens": 73},
                                   payment_resolution={"state": "paid", "verified_at": "old"}))
                count = len(store.list_dispatches(key))
                self.assertEqual(self.settle(store, key)["unchanged"], [key])
                after = store.get(key)
                self.assertEqual((after.status, after.message, after.output_path), (status, "unchanged", "/preserve/output.epub"))
                self.assertEqual(after.translation_stats, {"attempt_id": "old", "prompt_tokens": 73})
                self.assertEqual(after.payment_resolution, {"state": "paid", "verified_at": "old"})
                self.assertEqual(len(store.list_dispatches(key)), count)

    def test_reconciliation_candidates_include_old_expiry_but_not_arbitrary_cancel(self):
        for store in self.stores:
            for job in (
                self.job("waiting"), self.job("fresh", created_at=datetime.now(timezone.utc)),
                self.job("legacy", status=JobStatus.cancelled, message="支付超时，订单已关闭"),
                self.job("explicit", status=JobStatus.cancelled, error_code="PAYMENT_EXPIRED"),
                self.job("closed", status=JobStatus.cancelled, payment_resolution={"state": "closed"}),
                self.job("user", status=JobStatus.cancelled, message="用户取消"),
                self.job("review", status=JobStatus.cancelled, error_code="PAYMENT_EXPIRED", payment_resolution={"state": "paid_review"}),
            ):
                store.add(job)
            self.assertEqual({job.id for job in store.list_payment_reconciliation_candidates()}, {"waiting", "legacy", "explicit", "closed"})
            self.assertEqual([job.id for job in store.list_stale_pending_payment()], ["waiting"])

    def test_batch_requires_matching_real_leader(self):
        for store in self.stores:
            store.add(self.job("leader", batch_id="batch", batch_index=0))
            store.add(self.job("child", batch_id="batch", batch_index=1))
            empty = {"released": [], "review": [], "unchanged": []}
            self.assertEqual(self.settle(store, "child", batch_id="batch"), empty)
            self.assertEqual(self.settle(store, "leader", batch_id="different"), empty)
            self.assertEqual(self.settle(store, "missing", batch_id="batch"), empty)
            self.assertEqual(store.get("leader").status, JobStatus.pending_payment)
            self.assertEqual(store.list_dispatches(), [])

    def test_batch_late_receipt_releases_expiry_and_reviews_only_user_cancelled_child(self):
        for store in self.stores:
            for index in range(4):
                status = JobStatus.cancelled if index == 2 else JobStatus.success if index == 3 else JobStatus.pending_payment
                store.add(self.job(str(index), status=status, message="用户取消" if index == 2 else "",
                                   batch_id="batch", batch_index=index))
            self.assertEqual(store.mark_batch_payment_timeout("batch"), 0)
            self.assertEqual(store.mark_batch_payment_timeout("batch", gateway_confirmed=True), 2)
            self.assertEqual(self.settle(store, "0", batch_id="batch"),
                             {"released": ["0", "1"], "review": ["2"], "unchanged": ["3"]})
            self.assertEqual({row["job_id"] for row in store.list_dispatches()}, {"0", "1"})
            self.assertEqual(store.get("2").payment_resolution["state"], "paid_review")
            self.assertEqual(store.get("3").status, JobStatus.success)
            self.assertEqual(store.mark_batch_payment_timeout("batch", gateway_confirmed=True), 0)

    def test_concurrent_duplicate_settlements_produce_one_release_and_one_claim(self):
        for store in self.stores:
            store.add(self.job(enable_translation=True))
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.settle(store), range(12)))
            self.assertEqual(sum(len(result["released"]) for result in results), 1)
            self.assertEqual(sum(len(result["unchanged"]) for result in results), 11)
            self.assert_paid(store)
            at = timestamp() + 1
            with ThreadPoolExecutor(max_workers=8) as pool:
                claims = list(pool.map(lambda _: store.claim_dispatch(now=at), range(12)))
            self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_parallel_payment_and_gateway_close_always_end_paid(self):
        for store in self.stores:
            for index in range(8):
                key = str(index)
                store.add(self.job(key))
                barrier = threading.Barrier(2)
                def close():
                    barrier.wait()
                    return store.mark_payment_timeout(key, gateway_confirmed=True)
                def settle():
                    barrier.wait()
                    return self.settle(store, key)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    close_future, pay_future = pool.submit(close), pool.submit(settle)
                    close_future.result()
                    self.assertEqual(pay_future.result()["released"], [key])
                self.assert_paid(store, key)

    def test_parallel_batch_close_and_payment_lock_order_preserves_all_children(self):
        for store in self.stores:
            for index in range(3):
                store.add(self.job(str(index), batch_id="batch", batch_index=index))
            barrier = threading.Barrier(2)
            def close():
                barrier.wait()
                return store.mark_batch_payment_timeout("batch", gateway_confirmed=True)
            def settle():
                barrier.wait()
                return self.settle(store, "0", batch_id="batch")
            with ThreadPoolExecutor(max_workers=2) as pool:
                one, two = pool.submit(close), pool.submit(settle)
                one.result()
                self.assertEqual(two.result()["released"], ["0", "1", "2"])
            for index in range(3):
                self.assert_paid(store, str(index))

    def test_outbox_insert_failure_rolls_back_all_batch_dispositions_sql(self):
        for index in range(3):
            self.sql.add(self.job(str(index), batch_id="batch", batch_index=index,
                                  status=JobStatus.cancelled if index == 1 else JobStatus.pending_payment,
                                  message="用户取消" if index == 1 else ""))
        calls = 0
        def fail_second(_mapper, _connection, _target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected second outbox failure")
        event.listen(DispatchRecord, "before_insert", fail_second)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.settle(self.sql, "0", batch_id="batch")
        finally:
            event.remove(DispatchRecord, "before_insert", fail_second)
        self.assertEqual(self.sql.list_dispatches(), [])
        self.assertEqual([self.sql.get(str(i)).status for i in range(3)],
                         [JobStatus.pending_payment, JobStatus.cancelled, JobStatus.pending_payment])
        self.assertTrue(all(self.sql.get(str(i)).payment_resolution == {} for i in range(3)))
        self.assertEqual(self.sql.get("1").message, "用户取消")

    def test_preparation_failure_rolls_back_batch_resolution_memory(self):
        for index in range(3):
            self.memory.add(self.job(str(index), batch_id="batch", batch_index=index,
                                     status=JobStatus.cancelled if index == 1 else JobStatus.pending_payment))
        calls = 0
        def fail_second(job, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected preparation failure")
            return build_dispatch_intent(job, **kwargs)
        with patch("app.storage.build_dispatch_intent", side_effect=fail_second), self.assertRaisesRegex(RuntimeError, "injected"):
            self.settle(self.memory, "0", batch_id="batch")
        self.assertEqual(self.memory.list_dispatches(), [])
        self.assertTrue(all(self.memory.get(str(i)).payment_resolution == {} for i in range(3)))
        self.assertEqual(self.memory.get("0").status, JobStatus.pending_payment)

    def test_additive_migration_and_roundtrip_preserve_other_job_facts(self):
        self.sql.add(self.job(payment_entitlement={"state": "quoted"}, translation_stats={"old": 7}))
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE epub_jobs DROP COLUMN payment_resolution_json"))
        _ensure_compatible_schema(self.engine)
        old = self.sql.get("book")
        self.assertEqual(old.payment_resolution, {})
        self.assertEqual(old.payment_entitlement, {"state": "quoted"})
        self.assertEqual(old.translation_stats, {"old": 7})
        self.settle(self.sql)
        reopened = PersistentJobStore(self.engine)
        self.assertEqual(reopened.get("book").payment_resolution, self.sql.get("book").payment_resolution)
        self.assert_paid(reopened)

    def test_invalid_resolution_json_is_empty_not_inferred_paid(self):
        self.sql.add(self.job())
        for raw in ("not-json", "[]", "null"):
            with self.engine.begin() as connection:
                connection.execute(text("UPDATE epub_jobs SET payment_resolution_json=:value WHERE id='book'"), {"value": raw})
            self.assertEqual(self.sql.get("book").payment_resolution, {})

    def test_untrusted_settlement_source_cannot_mutate_job(self):
        for store in self.stores:
            store.add(self.job())
            with self.assertRaisesRegex(ValueError, "Untrusted"):
                store.settle_verified_payment("book", source="browser", amount="1.99")
            self.assertEqual(store.get("book").status, JobStatus.pending_payment)
            self.assertEqual(store.get("book").payment_resolution, {})
            self.assertEqual(store.list_dispatches(), [])


if __name__ == "__main__":
    unittest.main()
