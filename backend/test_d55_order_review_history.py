"""Opt-in real-book gate for stage-two manual order review.

The three SHA-pinned originals are actually uploaded and converted, without AI.
The historical cancelled row is an explicit restored-state fixture, not a claim
that the customer API permits cancelling pending-payment orders. Its subsequent
verified late callback, admin session/CSRF, review decision, durable dispatch,
worker, EPUBCheck and download all use real code. Only checkout/verified gateway
facts and the fault-injected broker transport are substitutes.

Original uploads, delivered artifacts and pre-B1 baselines remain read-only.
No live payment/refund, model, SMTP, production broker or server is accessed.
This is not a new translation or a live financial reconciliation acceptance.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import patch

import test_d39_table_history as previous
from test_d54_infra_history import BASELINE_SHA256, isolate_legacy_rate_limiter

navigation = previous.navigation


@unittest.skipUnless(all(os.environ.get(key) for key in (
    "EPUB_HISTORY_UPLOAD_DIR", "EPUB_HISTORY_OUTPUT_DIR", "EPUB_HISTORY_BASELINE_DIR")),
    "Explicit SHA-pinned upload, delivery and baseline directories required; no synthetic replacement.")
class OrderReviewHistoryTests(previous.TableHistoryTests):
    @classmethod
    def setUpClass(cls):
        # This guard must precede even the first app/engine import (dotenv and
        # the legacy limiter otherwise reach workspace-owned state).
        bootstrap = tempfile.TemporaryDirectory(prefix="epub-d55-bootstrap-")
        cls.addClassCleanup(bootstrap.cleanup)
        isolated = Path(bootstrap.name)
        cls.outer = ExitStack()
        cls.addClassCleanup(cls.outer.close)
        cls.workspace_databases = {}
        for suffix in ("", "-wal", "-shm"):
            path = Path(__file__).resolve().parent / ("rate_limit.db" + suffix)
            cls.workspace_databases[path] = navigation.sha256(path) if path.exists() else None
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "JAVA_HOME", "EPUBCHECK_JAR", "HOME", "TMPDIR"}
               or key.startswith("EPUB_HISTORY_")}
        env.update({
            "DATABASE_URL": "sqlite:///" + str(isolated / "bootstrap.db"),
            "EPUB_PERSISTENT_STORE": "1", "SKIP_PAYMENT_CHECK": "0",
            "EPUB_TRANSLATION_CHECKPOINT_DB": str(isolated / "checkpoints.db"),
            "REPAIR_UPLOAD_DIR": str(isolated / "repair"),
            "UPLOAD_DIR": str(isolated / "uploads"), "OUTPUT_DIR": str(isolated / "outputs"),
            "OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "", "DASHSCOPE_API_KEY": "",
            "GEMINI_API_KEY": "", "ALIPAY_APP_ID": "", "ALIPAY_SELLER_ID": "",
            "ALIPAY_PRIVATE_KEY": "", "ALIPAY_PUBLIC_KEY": "", "SENTRY_DSN": "",
            "SMTP_HOST": "", "NOTIFY_EMAIL_ENABLED": "0", "OWNER_PAYMENT_EMAIL_ENABLED": "0",
            "JOB_DISPATCH_ENABLED": "0", "CELERY_BROKER_URL": "", "REDIS_URL": "",
            "EPUB_LLM_RATE_LIMITER_ENABLED": "0", "EPUB_FAST_TRANSLATION": "1",
            "DOWNLOAD_SIGN_SECRET": "d55-isolated-signature", "ADMIN_USERNAME": "d55-auditor",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        cls.outer.enter_context(patch.dict(os.environ, env, clear=True))
        cls.outer.enter_context(patch("dotenv.load_dotenv", return_value=False))
        isolate_legacy_rate_limiter(cls.outer, isolated)
        cls.extra_guards = [cls.outer.enter_context(patch(target, side_effect=AssertionError(
            "D55 forbids external network, models and SMTP"))) for target in (
                "socket.getaddrinfo", "socket.socket.connect_ex", "socket.socket.sendto",
                "smtplib.SMTP", "smtplib.SMTP_SSL", "openai.OpenAI", "openai.AsyncOpenAI")]
        baseline = Path(os.environ["EPUB_HISTORY_BASELINE_DIR"]).resolve()
        for book in navigation.BOOKS:
            path = baseline / book["input_sha256"][:12] / "converted.epub"
            if navigation.sha256(path) != BASELINE_SHA256[book["key"]]:
                raise AssertionError("Historical baseline SHA changed: " + book["key"])
        super().setUpClass()
        from app import main, job_runner
        from app.admin import router
        from app.admin.auth import password_hash
        cls.main, cls.runner, cls.router = main, job_runner, router
        cls.outer.enter_context(patch.dict(os.environ, {
            "ADMIN_PASSWORD_HASH": password_hash("d55-local-only-password")}))

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for guard in cls.extra_guards:
            guard.assert_not_called()
        for path, expected in cls.workspace_databases.items():
            current = navigation.sha256(path) if path.exists() else None
            if current != expected:
                raise AssertionError("Workspace-owned rate-limit DB was modified")

    def setUp(self):
        from sqlalchemy import create_engine
        from app.storage_db import Base, PersistentJobStore
        self.case = tempfile.TemporaryDirectory(prefix="manual-review-", dir=self.root)
        self.addCleanup(self.case.cleanup)
        self.case_root = Path(self.case.name)
        self.case_uploads, self.case_outputs = self.case_root / "uploads", self.case_root / "outputs"
        self.case_uploads.mkdir()
        self.case_outputs.mkdir()
        self.engine = create_engine("sqlite:///" + str(self.case_root / "orders.db"))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for module, name, value in (
            (self.main, "job_store", self.store), (self.runner, "job_store", self.store),
            (self.main, "UPLOAD_DIR", self.case_uploads), (self.main, "OUTPUT_DIR", self.case_outputs),
            (self.runner, "OUTPUT_DIR", self.case_outputs),
        ):
            self.patches.enter_context(patch.object(module, name, value))
        self.patches.enter_context(patch("app.infra.execution_lease.tempfile.gettempdir",
                                         return_value=str(self.case_root)))
        self.patches.enter_context(patch.object(self.main, "_use_celery", return_value=True))
        self.patches.enter_context(patch("app.infra.alipay.create_alipay_precreate",
                                         return_value="https://offline.invalid/d55-qr"))
        self.patches.enter_context(patch.object(self.main, "verify_alipay_notification", return_value=True))
        self.trades = {}
        self.query = self.patches.enter_context(patch.object(
            self.router, "query_verified_trade", side_effect=lambda key: self.trades.get(key)))
        self.public_query = self.patches.enter_context(patch(
            "app.infra.alipay.query_verified_trade", side_effect=lambda key: self.trades.get(key)))
        self.queue, self.broker_available = [], False
        self.publisher = self.patches.enter_context(patch(
            "app.infra.job_dispatch_publisher.publish_conversion", side_effect=self._publish))
        self.executing = self.patches.enter_context(patch.object(
            self.runner, "_run_job_locked", wraps=self.runner._run_job_locked))
        self.client, self.csrf = self._client(login=True)

    def _client(self, *, login=False, cookies=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(self.router.make_router(
            self.store, self.case_uploads, self.case_outputs, self.main._enqueue_conversion))
        for path, endpoint, methods in (
            ("/api/v2/jobs", self.main.create_job_v2, ["POST"]),
            ("/api/v2/jobs/{job_id}", self.main.get_job_v2, ["GET"]),
            ("/api/v2/jobs/{job_id}/download", self.main.download_result_v2, ["GET"]),
            ("/api/v2/jobs/{job_id}/recover", self.main.recover_job_payment, ["POST"]),
            ("/api/v2/jobs/{job_id}/continue-payment", self.main.continue_job_payment_v2, ["POST"]),
            ("/api/v2/jobs/{job_id}/restart-translation", self.main.restart_translation_v2, ["POST"]),
            ("/api/v2/webhooks/alipay", self.main.alipay_webhook, ["POST"]),
        ):
            api.add_api_route(path, endpoint, methods=methods)
        client = TestClient(api, base_url="https://testserver")
        self.addCleanup(client.close)
        if cookies:
            client.cookies.update(cookies)
        csrf = None
        if login:
            response = client.post("/api/admin/login", json={
                "username": "d55-auditor", "password": "d55-local-only-password"})
            self.assertEqual(response.status_code, 200, response.text)
            csrf = response.json()["csrf"]
        return client, csrf

    def _reload(self):
        from app.storage_db import PersistentJobStore
        self.store = PersistentJobStore(self.engine)
        self.main.job_store = self.runner.job_store = self.store
        self.client, _ = self._client(cookies=self.client.cookies)

    def _publish(self, job_id, attempt_id):
        if not self.broker_available:
            raise ConnectionError("D55 controlled broker outage")
        self.queue.append((job_id, attempt_id))

    def _pending_review(self, book):
        from app.models import JobStatus
        with (self.uploads / book["input"]).open("rb") as source:
            response = self.client.post("/api/v2/jobs", files={
                "file": (book["input"], source, "application/epub+zip")}, data={
                "output_mode": "simplified", "enable_translation": "false",
                "enable_precision_polish": "false", "lexicon_domains_json": "[]",
                "enable_proper_noun": "false"})
        self.assertEqual(response.status_code, 200, response.text)
        job = self.store.get(response.json()["job_id"])
        self.assertEqual(job.status, JobStatus.pending_payment)
        self.assertFalse(job.is_test_order)
        self.assertEqual(navigation.sha256(Path(job.input_path)), book["input_sha256"])
        old = self.case_outputs / (job.id + "-historical-delivery.epub")
        shutil.copyfile(self.deliveries / book["output"], old)
        # Restored historical cancellation; no claim that pending_payment is a
        # cancellable customer-API state. Preserve an earlier delivered file.
        self.store.update_status(job.id, JobStatus.cancelled,
                                 "D55 restored historical user cancellation", output_path=str(old))
        self.trades[job.id] = {"out_trade_no": job.id, "trade_status": "TRADE_SUCCESS",
                               "total_amount": job.expected_amount, "trade_no": "offline-trade-" + job.id}
        self.assertEqual(self.client.post("/api/v2/webhooks/alipay", data=self.trades[job.id]).text, "success")
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.cancelled)
        self.assertEqual(job.payment_resolution["state"], "paid_review")
        self.assertEqual(self.store.list_dispatches(job.id), [])
        self.assertEqual(navigation.sha256(old), book["output_sha256"])
        return job, {"X-Job-Token": response.json()["access_token"]}, old

    def _review(self, job_id):
        response = self.client.get(f"/api/admin/orders/{job_id}")
        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()["review"]
        self.assertEqual(review["order_no"], job_id)
        return review

    def _body(self, job_id, action):
        review = self._review(job_id)
        return {"action": action, "request_id": str(uuid.uuid4()),
                "expected_revision": review["revision"], "expected_context": review["context"],
                "note": "d55-private-note-" + job_id, "evidence": "d55-private-evidence-" + job_id,
                "acknowledge_cost": action == "fulfill",
                "refund_reference": "d55-private-refund-" + job_id if action == "record_external_refund" else ""}

    def _apply(self, job_id, body, *, client=None, headers=None):
        return (client or self.client).post(f"/api/admin/orders/{job_id}/review", json=body,
            headers={"X-CSRF-Token": self.csrf} if headers is None else headers)

    def _assert_artifact(self, book, delivered, owner_headers):
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub
        result = validate_epub(delivered.output_path, EPUBCHECK_JAR)
        self.assertTrue(result.passed, "Actual historical EPUBCheck failed: " + book["key"])
        reference = self.runs[book["key"]][1]
        current = navigation.BookSnapshot(delivered.output_path, self.opencc)
        self.assertEqual(current.images, reference.images)
        self.assertEqual(set(current.docs), set(reference.docs))
        for name, expected in reference.docs.items():
            actual = current.docs[name]
            self.assertTrue(actual["text"] == expected["text"], "Historical body text changed: " + book["key"])
            self.assertEqual(actual["ids"], expected["ids"])
            signature = lambda link: (link["label"], link["target"], link["href"], link["disabled"])
            self.assertEqual(Counter(map(signature, actual["links"])), Counter(map(signature, expected["links"])))
        self.assertEqual([(row["label"], row["target"], row["depth"]) for row in current.toc],
                         [(row["label"], row["target"], row["depth"]) for row in reference.toc])
        public = self.client.get(f"/api/v2/jobs/{delivered.id}", headers=owner_headers)
        self.assertEqual(public.status_code, 200, public.text)
        self.assertEqual(public.json()["status"], "completed")
        for private in ("d55-auditor", "d55-private-note-", "d55-private-evidence-", "d55-private-refund-"):
            self.assertNotIn(private, public.text)
        response = self.client.get(public.json()["download_url"], headers=owner_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), navigation.sha256(Path(delivered.output_path)))

    def test_admin_fulfillment_three_books_survives_outage_ack_loss_reload_and_duplicate_delivery(self):
        from app.domain.job_dispatch_service import dispatch_pending
        from app.models import JobStatus
        for book in navigation.BOOKS:
            with self.subTest(book=book["key"]):
                self.broker_available = False
                self.queue.clear()
                self.publisher.reset_mock()
                self.executing.reset_mock()
                job, owner, old = self._pending_review(book)
                self.query.reset_mock()
                body = self._body(job.id, "fulfill")
                self.assertIn("fulfill", self._review(job.id)["allowed_actions"])
                response = self._apply(job.id, body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["review_action"]["duplicate"])
                self.query.assert_called_once_with(job.id)
                paid = self.store.get(job.id)
                self.assertEqual(paid.status, JobStatus.pending)
                attempt = paid.translation_stats["attempt_id"]
                self.assertTrue(attempt)
                self.assertEqual(paid.expected_amount, job.expected_amount)
                self.assertEqual(paid.input_path, job.input_path)
                first = self.store.list_dispatches(job.id)
                self.assertEqual(len(first), 1)
                self.assertEqual(first[0]["status"], "pending")
                self.assertEqual(first[0]["attempts"], 1)
                self.assertFalse(self.queue)
                self._reload()
                duplicate = self._apply(job.id, body)
                self.assertEqual(duplicate.status_code, 200, duplicate.text)
                self.assertTrue(duplicate.json()["review_action"]["duplicate"])
                self.query.assert_called_once_with(job.id)
                self.assertEqual(len(self.store.list_dispatches(job.id)), 1)
                self.assertEqual(self.store.get(job.id).translation_stats["attempt_id"], attempt)
                self.assertEqual(self.publisher.call_count, 1)
                self.broker_available = True
                with patch.object(self.store, "finish_dispatch", side_effect=RuntimeError("D55 lost broker acknowledgement")):
                    result = dispatch_pending(self.store, self.main._publish_conversion, job_id=job.id,
                                              now=first[0]["next_attempt_at"] + .1)
                self.assertEqual(result["published"], 1)
                self.assertEqual(result["sent"], 0)
                leased = self.store.get_dispatch(first[0]["dispatch_id"])
                self._reload()
                result = dispatch_pending(self.store, self.main._publish_conversion, job_id=job.id,
                                          now=leased["lease_expires_at"] + .1)
                self.assertEqual(result["sent"], 1)
                self.assertEqual(self.queue, [(job.id, attempt), (job.id, attempt)])
                with redirect_stdout(io.StringIO()):
                    for key, captured_attempt in self.queue:
                        self.runner.run_job(key, expected_attempt_id=captured_attempt)
                self.assertEqual(self.executing.call_count, 1)
                self._reload()
                delivered = self.store.get(job.id)
                self.assertEqual(delivered.status, JobStatus.success, delivered.message)
                self._assert_artifact(book, delivered, owner)
                self.assertEqual(navigation.sha256(old), book["output_sha256"])
                self.assertEqual(navigation.sha256(Path(job.input_path)), book["input_sha256"])
                history = self.client.get(f"/api/admin/orders/{job.id}/review-history")
                self.assertEqual(history.status_code, 200, history.text)
                self.assertEqual(len(history.json()["items"]), 1)
                self.assertEqual(history.json()["items"][0]["action"], "fulfill")
                self.assertEqual(history.json()["items"][0]["actor"], "d55-auditor")
                self.assertNotIn(str(old), history.text)
                self.assertNotIn(job.input_path, history.text)
                final_duplicate = self._apply(job.id, body)
                self.assertEqual(final_duplicate.status_code, 200, final_duplicate.text)
                self.assertTrue(final_duplicate.json()["review_action"]["duplicate"])
                self.query.assert_called_once_with(job.id)
                self.assertEqual(len(self.store.list_dispatches(job.id)), 1)

    def test_external_refund_record_three_books_never_releases_or_leaks_private_audit(self):
        from app.models import JobStatus
        for book in navigation.BOOKS:
            with self.subTest(book=book["key"]):
                job, owner, old = self._pending_review(book)
                self.query.reset_mock()
                body = self._body(job.id, "record_external_refund")
                response = self._apply(job.id, body)
                self.assertEqual(response.status_code, 200, response.text)
                self._reload()
                refunded = self.store.get(job.id)
                self.assertEqual(refunded.status, JobStatus.cancelled)
                self.assertEqual(refunded.payment_resolution["state"], "external_refund_recorded")
                self.assertTrue(refunded.payment_resolution["refund_recorded"])
                duplicate = self._apply(job.id, body)
                self.assertEqual(duplicate.status_code, 200, duplicate.text)
                self.assertTrue(duplicate.json()["review_action"]["duplicate"])
                self.query.assert_not_called()  # A manual record is not a gateway refund.
                self.assertEqual(self.client.post("/api/v2/webhooks/alipay", data=self.trades[job.id]).text, "success")
                for suffix in ("recover", "continue-payment", "restart-translation"):
                    result = self.client.post(f"/api/v2/jobs/{job.id}/{suffix}", headers=owner)
                    self.assertIn(result.status_code, (200, 400, 409), result.text)
                retry = self.client.post(f"/api/admin/orders/{job.id}/retry",
                    json={"acknowledge_cost": True}, headers={"X-CSRF-Token": self.csrf})
                self.assertEqual(retry.status_code, 409, retry.text)
                denied = self._apply(job.id, self._body(job.id, "fulfill"))
                self.assertEqual(denied.status_code, 409, denied.text)
                refresh = self.client.post(f"/api/admin/orders/{job.id}/payment", headers={"X-CSRF-Token": self.csrf})
                self.assertEqual(refresh.status_code, 200, refresh.text)
                public = self.client.get(f"/api/v2/jobs/{job.id}", headers=owner)
                self.assertEqual(public.status_code, 200, public.text)
                for private in ("d55-auditor", body["note"], body["evidence"], body["refund_reference"]):
                    self.assertNotIn(private, public.text)
                after = self.store.get(job.id)
                self.assertEqual(after.status, JobStatus.cancelled)
                self.assertTrue(after.payment_resolution["refund_recorded"])
                self.assertEqual(after.expected_amount, job.expected_amount)
                self.assertEqual(after.input_path, job.input_path)
                self.assertEqual(after.output_path, str(old))
                self.assertEqual(after.translation_stats, refunded.translation_stats)
                self.assertEqual(self.store.list_dispatches(job.id), [])
                self.assertEqual(navigation.sha256(old), book["output_sha256"])
                download = self.client.get(f"/api/admin/orders/{job.id}/files/output")
                self.assertEqual(download.status_code, 200)
                self.assertEqual(hashlib.sha256(download.content).hexdigest(), book["output_sha256"])
                history = self.client.get(f"/api/admin/orders/{job.id}/review-history")
                self.assertEqual(history.status_code, 200)
                self.assertEqual(len(history.json()["items"]), 1)
                self.assertEqual(history.json()["items"][0]["action"], "record_external_refund")
                self.assertEqual(history.json()["items"][0]["actor"], "d55-auditor")
                self.assertEqual(history.json()["items"][0]["refund_reference"], body["refund_reference"])
        self.publisher.assert_not_called()
        self.executing.assert_not_called()

    def test_real_book_review_requires_session_csrf_current_snapshot_and_cost_acknowledgement(self):
        for book in navigation.BOOKS:
            with self.subTest(book=book["key"]):
                job, _, old = self._pending_review(book)
                self.query.reset_mock()
                body = self._body(job.id, "fulfill")
                anonymous, _ = self._client()
                self.assertEqual(self._apply(job.id, body, client=anonymous).status_code, 401)
                self.assertEqual(self._apply(job.id, body, headers={}).status_code, 403)
                self.assertEqual(self._apply(job.id, body, headers={
                    "X-CSRF-Token": self.csrf, "Origin": "https://other.invalid"}).status_code, 403)
                self.assertEqual(self._apply(job.id, {**body, "actor": "forged"}).status_code, 422)
                self.query.assert_not_called()
                self.assertEqual(self._apply(job.id, {**body, "expected_revision": body["expected_revision"] + 1}).status_code, 409)
                self.assertEqual(self._apply(job.id, {**body, "expected_context": "0" * 64}).status_code, 409)
                no_cost = self._apply(job.id, {**body, "acknowledge_cost": False})
                self.assertIn(no_cost.status_code, (409, 422), no_cost.text)
                self.assertEqual(self.store.get(job.id), job)
                self.assertEqual(self.store.list_dispatches(job.id), [])
                self.assertEqual(navigation.sha256(old), book["output_sha256"])
        self.publisher.assert_not_called()
        self.executing.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
