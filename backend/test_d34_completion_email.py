"""Offline regression: durable opt-in mail, safe links, SMTP and bounded retries."""
import json
import os
import smtplib
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
from sqlalchemy import create_engine
from app.domain import completion_email_service as service
from app.domain.email_subscription_repository import EmailSubscriptionRepository
from app.models import Job, JobStatus, OutputMode
from app.storage import JobStore
from app.storage_db import Base, PersistentJobStore

CONFIG = dict(NOTIFY_EMAIL_ENABLED="1", SMTP_HOST="mail.example.com", SMTP_PORT="465",
              SMTP_USER="sender@example.com", SMTP_PASSWORD="offline-password", SMTP_SECURITY="ssl",
              SMTP_TIMEOUT_SECONDS="12", SITE_BASE_URL="https://fixepub.com")


class CompletionEmailTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, CONFIG, clear=True))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network prohibited")))
        self.store = JobStore()
        self.stack.enter_context(patch.object(service, "job_store", self.store))
        self.send = self.stack.enter_context(patch.object(service, "_send_email"))
        self.repo = EmailSubscriptionRepository(self.store)

    def job(self, job_id="email-job", status=JobStatus.running):
        job = Job(id=job_id, source_filename="private-book.epub", output_mode=OutputMode.simplified,
                  trace_id="trace", input_path="/secret/source.epub", access_token="private-access-token",
                  status=status, output_path="/secret/output.epub", message="raw error password=secret")
        self.store.add(job)
        return job

    def subscribe(self, job_id="email-job", email="reader@example.com"):
        return service.set_email_subscription(job_id, email)

    def test_disabled_does_not_fake_subscription(self):
        os.environ.pop("SMTP_PASSWORD")
        self.assertFalse(service.email_capabilities()["available"])
        with self.assertRaises(service.EmailUnavailableError):
            self.subscribe()
        self.assertIsNone(self.repo.get("email-job"))
        self.assertEqual(service.dispatch_pending_email_notifications()["unavailable"], 1)
        self.send.assert_not_called()

    def test_email_validation_and_normalization(self):
        for address in ["a@b", "a\r\nBcc:bad@example.com", ".a@example.com", "a..b@example.com", "甲@example.com", "a" * 65 + "@example.com"]:
            with self.assertRaises(ValueError):
                self.subscribe(email=address)
        self.assertEqual(self.subscribe(email=" reader@EXAMPLE.COM ")["email"], "reader@example.com")

    def test_legacy_global_destination_never_subscribes(self):
        self.job(status=JobStatus.success)
        os.environ["NOTIFY_EMAIL_TO"] = "admin@example.com"
        self.assertFalse(service.queue_completion_email("email-job", "success", source_filename="private"))
        service.dispatch_pending_email_notifications()
        self.send.assert_not_called()

    def test_completed_subscribe_delivers_safe_link_once(self):
        self.job(status=JobStatus.success)
        self.assertEqual(self.subscribe()["status"], "pending")
        self.assertEqual(service.dispatch_pending_email_notifications()["sent"], 1)
        self.assertEqual(service.dispatch_pending_email_notifications()["sent"], 0)
        self.send.assert_called_once()
        body = self.send.call_args.args[2]
        self.assertIn("https://fixepub.com/?job_id=email-job#access_token=private-access-token", body)
        for private in ["/secret/", "private-book", "password=secret", "reader@example.com"]:
            self.assertNotIn(private, body)
        self.assertEqual(service.get_email_subscription("email-job")["status"], "sent")
        self.assertTrue(service.get_email_subscription("email-job")["sent_at"])

    def test_failure_subject_and_body_not_success_or_raw_error(self):
        self.job(status=JobStatus.failed)
        self.subscribe()
        service.dispatch_pending_email_notifications()
        self.assertNotIn("已完成", self.send.call_args.args[1])
        self.assertIn("暂未完成", self.send.call_args.args[2])
        self.assertNotIn("password=secret", self.send.call_args.args[2])

    def test_running_subscription_does_not_send(self):
        self.job()
        self.subscribe()
        self.assertFalse(service.queue_completion_email("email-job", "success"))
        service.dispatch_pending_email_notifications()
        self.send.assert_not_called()

    def test_old_job_without_access_token_has_explicit_failure(self):
        self.job(status=JobStatus.success).access_token = ""
        state = self.subscribe()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_error_code"], "result_link_unavailable")
        service.dispatch_pending_email_notifications()
        self.send.assert_not_called()

    def test_expired_token_does_not_send_or_extend_expiry(self):
        job = self.job(status=JobStatus.success)
        expired = datetime.now(timezone.utc) - timedelta(days=1)
        job.token_expires_at = expired
        state = self.subscribe()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_error_code"], "result_link_unavailable")
        service.dispatch_pending_email_notifications()
        self.send.assert_not_called()
        self.assertEqual(job.token_expires_at, expired)

    def test_token_expiring_after_queue_is_rechecked_before_send(self):
        job = self.job(status=JobStatus.success)
        job.token_expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertEqual(self.subscribe()["status"], "pending")
        # SQLite can return naive UTC datetimes, so cover that representation too.
        job.token_expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(tzinfo=None)
        service.dispatch_pending_email_notifications()
        self.send.assert_not_called()
        self.assertEqual(service.get_email_subscription("email-job")["last_error_code"], "result_link_unavailable")

    def test_reading_subscription_does_not_mutate_or_send(self):
        self.job(status=JobStatus.success)
        self.subscribe()
        before = self.repo.get("email-job")
        service.get_email_subscription("email-job")
        self.assertEqual(self.repo.get("email-job"), before)
        self.send.assert_not_called()

    def test_missed_completion_hook_reconciles(self):
        self.job()
        self.subscribe()
        self.store.update_status("email-job", JobStatus.success, "ok")
        self.assertEqual(service.dispatch_pending_email_notifications()["sent"], 1)

    def test_duplicate_hook_and_duplicate_save_do_not_resend(self):
        self.job(status=JobStatus.success)
        self.subscribe()
        service.dispatch_pending_email_notifications()
        for _ in range(5):
            self.subscribe()
            service.queue_completion_email("email-job", "success")
            service.dispatch_pending_email_notifications()
        self.send.assert_called_once()
        self.assertEqual(self.repo.get("email-job")["address_changes"], 1)

    def test_failure_backoff_then_success(self):
        self.job(status=JobStatus.success)
        self.subscribe()
        self.send.side_effect = [TimeoutError("private provider text"), None]
        with patch.object(service.time, "time", return_value=1000):
            self.assertEqual(service.dispatch_pending_email_notifications()["retried"], 1)
        self.assertEqual(self.repo.get("email-job")["last_error_code"], "smtp_connection_failed")
        with patch.object(service.time, "time", return_value=1059):
            service.dispatch_pending_email_notifications()
        self.assertEqual(self.send.call_count, 1)
        with patch.object(service.time, "time", return_value=1061):
            self.assertEqual(service.dispatch_pending_email_notifications()["sent"], 1)
        self.assertEqual(self.send.call_count, 2)

    def test_five_attempt_limit_survives_repeated_save(self):
        self.job(status=JobStatus.success)
        self.subscribe()
        self.send.side_effect = RuntimeError("smtp password=secret recipient=private")
        for now in [1000, 2000, 3000, 4000, 5000, 9000, 15000]:
            with patch.object(service.time, "time", return_value=now):
                service.dispatch_pending_email_notifications()
                self.subscribe()
        self.assertEqual(self.send.call_count, 5)
        self.assertEqual(self.repo.get("email-job")["status"], "failed")
        self.assertEqual(self.repo.get("email-job")["last_error_code"], "smtp_delivery_failed")

    def test_unsubscribe_cancels_outbox_even_when_service_disabled(self):
        self.job(status=JobStatus.success)
        self.subscribe()
        os.environ["NOTIFY_EMAIL_ENABLED"] = "0"
        self.assertFalse(self.subscribe(email="")["enabled"])
        os.environ["NOTIFY_EMAIL_ENABLED"] = "1"
        service.dispatch_pending_email_notifications()
        self.send.assert_not_called()

    def test_address_changes_and_resends_bounded(self):
        self.job(status=JobStatus.success)
        for i in range(3):
            self.subscribe(email=f"reader{i}@example.com")
            service.dispatch_pending_email_notifications()
        with self.assertRaises(service.EmailRateLimitError):
            self.subscribe(email="fourth@example.com")
        self.assertEqual(self.send.call_count, 3)

    def test_batch_change_all_or_nothing(self):
        self.job("fresh")
        self.job("limited")
        for i in range(3):
            self.subscribe("limited", f"reader{i}@example.com")
        with self.assertRaises(service.EmailRateLimitError):
            service.set_email_subscriptions(["fresh", "limited"], "new@example.com")
        self.assertIsNone(self.repo.get("fresh"))
        self.assertEqual(self.repo.get("limited")["email"], "reader2@example.com")

    def test_batch_subscription_limit_matches_configured_upload_limit(self):
        os.environ["BATCH_MAX_FILES"] = "12"
        job_ids = [f"batch-job-{index}" for index in range(12)]
        saved = service.set_email_subscriptions(job_ids, "reader@example.com")
        self.assertEqual(len(saved), 12)
        self.assertTrue(all(row["enabled"] for row in saved))
        with self.assertRaises(ValueError):
            service.set_email_subscriptions(job_ids + ["extra"], "reader@example.com")
        self.assertIsNone(self.repo.get("extra"))

    def test_retry_supersedes_pending_failure_mail(self):
        self.job(status=JobStatus.failed)
        self.subscribe()
        self.store.update_status("email-job", JobStatus.running, "retry")
        service.dispatch_pending_email_notifications()
        self.send.assert_not_called()
        self.assertEqual(self.repo.get("email-job")["status"], "subscribed")
        self.store.update_status("email-job", JobStatus.success, "ok")
        service.dispatch_pending_email_notifications()
        self.send.assert_called_once()
        self.assertIn("已完成", self.send.call_args.args[1])

    def test_expired_lease_is_recovered(self):
        self.job(status=JobStatus.success)
        self.subscribe()
        def claim(old):
            old.update(status="sending", lease_id="dead-worker", lease_until=1000, attempts=1)
            return old
        self.repo.mutate("email-job", claim)
        with patch.object(service.time, "time", return_value=999):
            service.dispatch_pending_email_notifications()
        self.send.assert_not_called()
        with patch.object(service.time, "time", return_value=1001):
            service.dispatch_pending_email_notifications()
        self.send.assert_called_once()

    def test_concurrent_dispatchers_only_one_sends(self):
        self.job(status=JobStatus.success)
        self.subscribe()
        entered, release = threading.Event(), threading.Event()
        self.send.side_effect = lambda *_: (entered.set(), release.wait(3))
        worker = threading.Thread(target=service.dispatch_pending_email_notifications)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            service.dispatch_pending_email_notifications()
            self.assertEqual(self.send.call_count, 1)
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(self.repo.get("email-job")["status"], "sent")

    def test_persistent_restart_and_atomic_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine("sqlite:///" + str(Path(tmp) / "mail.db"))
            Base.metadata.create_all(engine)
            self.store = PersistentJobStore(engine=engine)
            with patch.object(service, "job_store", self.store):
                self.job(status=JobStatus.success)
                self.subscribe()
            with patch.object(service, "job_store", PersistentJobStore(engine=engine)):
                self.assertEqual(service.dispatch_pending_email_notifications()["sent"], 1)
                self.assertEqual(service.get_email_subscription("email-job")["status"], "sent")
                self.store.add(Job(id="limited", source_filename="x", output_mode=OutputMode.simplified, trace_id="x", input_path="x"))
                for i in range(3):
                    service.set_email_subscription("limited", f"r{i}@example.com")
                with self.assertRaises(service.EmailRateLimitError):
                    service.set_email_subscriptions(["fresh", "limited"], "new@example.com")
                self.assertIsNone(EmailSubscriptionRepository(self.store).get("fresh"))
            engine.dispose()

    def test_persistent_concurrent_dispatchers_share_one_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine("sqlite:///" + str(Path(tmp) / "mail.db"))
            Base.metadata.create_all(engine)
            self.store = PersistentJobStore(engine=engine)
            with patch.object(service, "job_store", self.store):
                self.job(status=JobStatus.success)
                self.subscribe()
                entered, release = threading.Event(), threading.Event()
                self.send.side_effect = lambda *_: (entered.set(), release.wait(3))
                worker = threading.Thread(target=service.dispatch_pending_email_notifications)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    service.dispatch_pending_email_notifications()
                    self.assertEqual(self.send.call_count, 1)
                finally:
                    release.set()
                    worker.join(3)
                self.assertEqual(service.get_email_subscription("email-job")["status"], "sent")
            engine.dispose()

    def test_cancelled_result_has_correct_subject_and_body(self):
        self.job(status=JobStatus.cancelled)
        self.subscribe()
        service.dispatch_pending_email_notifications()
        self.assertNotIn("已完成", self.send.call_args.args[1])
        self.assertIn("已取消", self.send.call_args.args[2])

    def test_provider_exception_never_saved_or_logged_raw(self):
        self.job(status=JobStatus.success)
        self.subscribe()
        self.send.side_effect = smtplib.SMTPAuthenticationError(535, b"password=very-private reader@example.com")
        with self.assertLogs("epub_factory", level="WARNING") as captured:
            service.dispatch_pending_email_notifications()
        self.assertNotIn("very-private", str(captured.output))
        self.assertNotIn("reader@example.com", str(captured.output))
        self.assertEqual(service.get_email_subscription("email-job")["last_error_code"], "smtp_authentication_failed")

    def test_repair_completion_is_reconciled_from_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["REPAIR_UPLOAD_DIR"] = tmp
            job_id = "a" * 32
            directory = Path(tmp) / job_id
            directory.mkdir()
            (directory / "order.json").write_text(json.dumps({"status": "repaired", "filename": "secret.epub"}))
            self.subscribe("repair:" + job_id)
            service.dispatch_pending_email_notifications()
            self.assertIn("https://fixepub.com/epub-repair.html?job_id=" + job_id, self.send.call_args.args[2])
            self.assertNotIn("secret.epub", self.send.call_args.args[2])

    def test_invalid_smtp_configuration_is_unavailable(self):
        for key, value in [("SMTP_PORT", "wrong"), ("SMTP_SECURITY", "plain"), ("SMTP_TIMEOUT_SECONDS", "0"), ("SITE_BASE_URL", "https://evil.example/?leak=1"), ("SMTP_FROM", "a\nBcc:bad@example.com")]:
            with patch.dict(os.environ, {key: value}):
                self.assertFalse(service.email_capabilities()["available"])


class SmtpTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, CONFIG, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("network prohibited"))
        network.start()
        self.addCleanup(network.stop)

    def test_ssl_timeout_login_and_message(self):
        with patch.object(service.smtplib, "SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            service._send_email("reader@example.com", "结果", "固定正文", "<id@fixepub.com>")
            self.assertEqual(smtp.call_args.kwargs["timeout"], 12)
            client = smtp.return_value.__enter__.return_value
            client.login.assert_called_once_with("sender@example.com", "offline-password")
            client.starttls.assert_not_called()
            self.assertEqual(client.send_message.call_args.args[0]["To"], "reader@example.com")
            self.assertEqual(client.send_message.call_args.args[0]["Message-ID"], "<id@fixepub.com>")

    def test_starttls_and_refused_recipient(self):
        os.environ.update(SMTP_PORT="587", SMTP_SECURITY="starttls")
        with patch.object(service.smtplib, "SMTP") as smtp:
            client = smtp.return_value.__enter__.return_value
            client.send_message.return_value = {"reader@example.com": (550, b"refused")}
            with self.assertRaises(smtplib.SMTPRecipientsRefused):
                service._send_email("reader@example.com", "结果", "正文", "<id@fixepub.com>")
            client.starttls.assert_called_once()
            self.assertEqual(client.ehlo.call_count, 2)


if __name__ == "__main__":
    unittest.main()
