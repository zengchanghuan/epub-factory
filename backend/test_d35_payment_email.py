"""Offline merchant mail regression; no model, SMTP connection or live orders."""
import os
import smtplib
import socket
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from sqlalchemy import create_engine
from app.domain import completion_email_service as completion
from app.domain import payment_email_service as service
from app.domain import payment_email_worker as worker_module
from app.domain.payment_email_repository import PaymentEmailRepository
from app.storage import JobStore
from app.storage_db import Base, PersistentJobStore

CONFIG = dict(NOTIFY_EMAIL_ENABLED="0", OWNER_PAYMENT_EMAIL_ENABLED="1", SMTP_HOST="smtp.example.com",
              SMTP_PORT="465", SMTP_USER="sender@example.com", SMTP_PASSWORD="offline-only",
              SMTP_SECURITY="ssl", SITE_BASE_URL="https://fixepub.com")


class PaymentEmailTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, CONFIG, clear=True))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network prohibited")))
        self.store = JobStore()
        self.stack.enter_context(patch.object(service, "job_store", self.store))
        self.send = self.stack.enter_context(patch.object(completion, "_send_email"))
        self.wake = self.stack.enter_context(patch.object(worker_module.payment_email_worker, "wake"))
        self.repo = PaymentEmailRepository(self.store)

    def queue(self, order_no="order-123", **kwargs):
        return service.queue_paid_order_email(order_no, kwargs.pop("amount", "1.99"),
                                             kwargs.pop("order_kind", "conversion"), **kwargs)

    def persistent_store(self):
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        engine = create_engine("sqlite:///" + str(Path(directory) / "outbox.db"), connect_args={"timeout": 15})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.store = PersistentJobStore(engine=engine)
        self.stack.enter_context(patch.object(service, "job_store", self.store))
        self.repo = PaymentEmailRepository(self.store)
        return engine

    def test_queue_is_durable_before_wake_and_never_calls_smtp(self):
        def check_saved():
            self.assertEqual(self.repo.get("order-123")["status"], "pending")
        self.wake.side_effect = check_saved
        self.assertTrue(self.queue())
        self.wake.assert_called_once()
        self.send.assert_not_called()

    def test_duplicate_callbacks_and_batch_books_send_one_whole_order_receipt(self):
        self.assertTrue(self.queue("batch_123", amount="19.90", order_kind="batch", file_count=10))
        for _ in range(10):
            self.assertFalse(self.queue("batch_123", amount="1.99", file_count=1))
        self.assertEqual(service.dispatch_pending_payment_emails()["sent"], 1)
        self.assertEqual(service.dispatch_pending_payment_emails()["sent"], 0)
        self.send.assert_called_once()
        self.assertIn("¥19.90", self.send.call_args.args[2])
        self.assertIn("10 本", self.send.call_args.args[2])
        self.assertEqual(self.repo.get("batch_123")["status"], "sent")

    def test_default_recipient_and_china_time_and_safe_admin_link(self):
        self.assertTrue(self.queue(order_kind="repair", paid_at=datetime(2026, 9, 18, 1, 2, 3, tzinfo=timezone.utc)))
        service.dispatch_pending_payment_emails()
        args = self.send.call_args.args
        self.assertEqual(args[0], "249998620@qq.com")
        self.assertIn("付款", args[1])
        for expected in ["¥1.99", "order-123", "EPUB 修复", "2026-09-18 09:02:03 北京时间",
                         "https://fixepub.com/orders-admin.html", "管理员登录"]:
            self.assertIn(expected, args[2])
        for secret in ["offline-only", "sender@example.com", "access_token", "private-book", "输出文件"]:
            self.assertNotIn(secret, args[2])
        self.assertTrue(self.repo.get("order-123")["sent_at"])

    def test_naive_utc_and_iso_timestamps(self):
        for order, paid_at in [("naive", datetime(2026, 9, 18, 1)), ("iso", "2026-09-18T01:00:00Z")]:
            self.queue(order, paid_at=paid_at)
            self.assertEqual(self.repo.get(order)["payload"]["paid_at"], "2026-09-18 09:00:00 北京时间")

    def test_customer_mail_can_be_disabled_independently(self):
        self.assertFalse(completion.email_capabilities()["available"])
        self.assertTrue(service.payment_email_capabilities()["available"])
        self.queue()
        self.assertEqual(service.dispatch_pending_payment_emails()["sent"], 1)
        self.assertEqual(self.send.call_args.kwargs, {"require_enabled": False})

    def test_owner_disabled_and_test_orders_are_not_queued(self):
        self.assertFalse(self.queue(is_test_order=True))
        with patch.dict(os.environ, OWNER_PAYMENT_EMAIL_ENABLED="0"):
            self.assertFalse(self.queue())
            self.assertFalse(service.payment_email_capabilities()["available"])
        self.assertIsNone(self.repo.get("order-123"))
        self.wake.assert_not_called()

    def test_disabled_owner_pauses_existing_queue(self):
        self.queue()
        with patch.dict(os.environ, OWNER_PAYMENT_EMAIL_ENABLED="0"):
            self.assertEqual(service.dispatch_pending_payment_emails()["unavailable"], 1)
        self.assertEqual(self.repo.get("order-123")["attempts"], 0)
        self.send.assert_not_called()

    def test_unconfigured_smtp_retains_new_receipt_without_consuming_retries(self):
        os.environ.pop("SMTP_PASSWORD")
        self.assertTrue(self.queue())
        for _ in range(3):
            self.assertEqual(service.dispatch_pending_payment_emails()["unavailable"], 1)
        self.assertEqual(self.repo.get("order-123")["attempts"], 0)
        self.send.assert_not_called()
        os.environ["SMTP_PASSWORD"] = "offline-only"
        self.assertEqual(service.dispatch_pending_payment_emails()["sent"], 1)

    def test_invalid_inputs_are_contained(self):
        invalid = [(None, {}), ("evil\nBcc", {}), ("a" * 101, {}), ("valid", {"amount": "NaN"}),
                   ("valid", {"amount": "Infinity"}), ("valid", {"amount": "-1"}),
                   ("valid", {"amount": "0"}), ("valid", {"amount": "1.991"}),
                   ("valid", {"file_count": True}), ("valid", {"file_count": 0}),
                   ("valid", {"file_count": "1"}), ("valid", {"paid_at": "bad"})]
        with self.assertLogs("epub_factory", level="WARNING"):
            for order, kwargs in invalid:
                self.assertFalse(self.queue(order, **kwargs))
        self.assertEqual(self.repo.due(time.time()), [])

    def test_invalid_owner_recipient_is_never_sent(self):
        for address in ["", "249998620qq.com", "a@example.com\r\nBcc: bad@example.com"]:
            with patch.dict(os.environ, OWNER_PAYMENT_EMAIL_TO=address), self.assertLogs("epub_factory", level="WARNING"):
                self.assertFalse(self.queue())
                self.assertFalse(service.payment_email_capabilities()["available"])
        self.send.assert_not_called()

    def test_untrusted_kind_is_not_embedded_in_email(self):
        self.queue(order_kind="raw error: secret private-book.epub")
        service.dispatch_pending_payment_emails()
        self.assertNotIn("secret", self.send.call_args.args[2])
        self.assertIn("订单类型：订单", self.send.call_args.args[2])

    def test_limited_retry_backoff_and_stable_message_id(self):
        self.queue()
        self.send.side_effect = TimeoutError("provider-secret")
        clock = time.time() + 1
        for attempt in range(1, 6):
            with patch.object(service.time, "time", return_value=clock), self.assertLogs("epub_factory", level="WARNING"):
                result = service.dispatch_pending_payment_emails()
            row = self.repo.get("order-123")
            self.assertEqual(row["attempts"], attempt)
            self.assertEqual(result["retried" if attempt < 5 else "failed"], 1)
            if attempt < 5:
                self.assertEqual(row["next_attempt_at"], clock + 60 * 2 ** (attempt - 1))
                with patch.object(service.time, "time", return_value=clock + 1):
                    self.assertEqual(service.dispatch_pending_payment_emails()["retried"], 0)
                clock = row["next_attempt_at"]
        self.assertEqual(self.repo.get("order-123")["status"], "failed")
        self.assertEqual(self.send.call_count, 5)
        self.assertEqual(len({call.args[3] for call in self.send.call_args_list}), 1)
        self.assertEqual(service.dispatch_pending_payment_emails()["sent"], 0)

    def test_raw_provider_exception_not_saved_or_logged(self):
        self.queue()
        self.send.side_effect = smtplib.SMTPAuthenticationError(535, b"secret-password recipient@example.com")
        with self.assertLogs("epub_factory", level="WARNING") as captured:
            service.dispatch_pending_payment_emails()
        for secret in ["secret-password", "recipient@example.com"]:
            self.assertNotIn(secret, str(captured.output))
            self.assertNotIn(secret, str(self.repo.get("order-123")))
        self.assertEqual(self.repo.get("order-123")["last_error_code"], "smtp_authentication_failed")

    def test_queue_storage_error_does_not_escape_payment_path(self):
        with patch.object(PaymentEmailRepository, "mutate", side_effect=RuntimeError("private-dsn")), self.assertLogs("epub_factory", level="WARNING") as captured:
            self.assertFalse(self.queue())
        self.assertNotIn("private-dsn", str(captured.output))
        self.send.assert_not_called()

    def test_wake_failure_keeps_durable_receipt(self):
        self.wake.side_effect = RuntimeError("private")
        with self.assertLogs("epub_factory", level="WARNING"):
            self.assertTrue(self.queue())
        self.assertEqual(self.repo.get("order-123")["status"], "pending")

    def test_no_scanning_old_jobs_or_customer_subscriptions(self):
        with patch.object(self.store, "list_jobs", side_effect=AssertionError("must not scan")):
            self.assertEqual(service.dispatch_pending_payment_emails()["sent"], 0)
        self.assertFalse(hasattr(self.store, "_email_subscriptions"))
        self.send.assert_not_called()

    def test_persistence_and_duplicates_survive_restart(self):
        engine = self.persistent_store()
        self.assertTrue(self.queue())
        restarted = PersistentJobStore(engine=engine)
        with patch.object(service, "job_store", restarted):
            self.assertFalse(self.queue())
            self.assertEqual(service.dispatch_pending_payment_emails()["sent"], 1)
        self.assertEqual(self.repo.get("order-123")["status"], "sent")
        self.assertFalse(self.queue())
        self.assertEqual(self.repo.due(time.time()), [])

    def test_persistent_concurrent_callbacks_create_once(self):
        self.persistent_store()
        barrier = threading.Barrier(6)
        def enqueue(_):
            barrier.wait(timeout=2)
            return self.queue()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(enqueue, range(6)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.repo.due(time.time())), 1)

    def test_persistent_concurrent_dispatch_uses_lease(self):
        self.persistent_store()
        self.queue()
        sending = threading.Event()
        release = threading.Event()
        def send(*args, **kwargs):
            sending.set()
            self.assertTrue(release.wait(3))
        self.send.side_effect = send
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.dispatch_pending_payment_emails)
            try:
                self.assertTrue(sending.wait(2))
                second = pool.submit(service.dispatch_pending_payment_emails)
                self.assertEqual(second.result(timeout=2)["sent"], 0)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2)["sent"], 1)
        self.send.assert_called_once()

    def test_expired_lease_can_resume_but_max_attempts_stops_crash_loop(self):
        self.queue()
        def abandoned(old):
            old.update(status="sending", lease_id="dead", lease_until=time.time() - 1, attempts=1)
            return old
        self.repo.mutate("order-123", abandoned)
        self.assertEqual(service.dispatch_pending_payment_emails()["sent"], 1)
        self.queue("exhausted")
        def exhausted(old):
            old.update(status="sending", lease_id="dead", lease_until=0, attempts=5)
            return old
        self.repo.mutate("exhausted", exhausted)
        self.assertEqual(service.dispatch_pending_payment_emails()["failed"], 1)
        self.assertEqual(self.repo.get("exhausted")["last_error_code"], "delivery_attempts_exhausted")
        self.send.assert_called_once()


class PaymentWorkerTests(unittest.TestCase):
    def test_disabled_config_does_not_start_thread(self):
        worker = worker_module.PaymentEmailWorker()
        with patch.object(worker_module, "payment_email_capabilities", return_value={"available": False}):
            worker.start()
        self.assertIsNone(worker._thread)

    def test_wake_dispatches_immediately_without_waiting_for_book_or_scan(self):
        worker = worker_module.PaymentEmailWorker()
        dispatched = threading.Event()
        with patch.object(worker_module, "payment_email_capabilities", return_value={"available": True}), \
                patch.object(worker_module, "dispatch_pending_payment_emails", side_effect=lambda **kwargs: dispatched.set()) as dispatch:
            worker.start()
            try:
                self.assertTrue(dispatched.wait(1))
                dispatched.clear()
                thread = worker._thread
                worker.start()
                self.assertIs(worker._thread, thread)
                worker.wake()
                self.assertTrue(dispatched.wait(1))
                self.assertGreaterEqual(dispatch.call_count, 2)
                self.assertTrue(worker._thread.daemon)
            finally:
                worker.stop()
        self.assertFalse(worker._thread.is_alive())

    def test_dispatcher_failure_is_contained_without_leaking_raw_error(self):
        worker = worker_module.PaymentEmailWorker()
        called = threading.Event()
        def fail(**kwargs):
            called.set()
            raise RuntimeError("private-dsn")
        with patch.object(worker_module, "payment_email_capabilities", return_value={"available": True}), \
                patch.object(worker_module, "dispatch_pending_payment_emails", side_effect=fail), \
                self.assertLogs("epub_factory", level="WARNING") as captured:
            worker.start()
            try:
                self.assertTrue(called.wait(1))
            finally:
                worker.stop()
        self.assertNotIn("private-dsn", str(captured.output))


if __name__ == "__main__":
    unittest.main()
