"""R3 payment/execution contract gates, using temporary stores and no network.

The HTTP endpoints, persistent store, payment-fact checks, execution lease and
runner are real. Payment providers, the ordinary converter and precision model
service are replaced at their external boundaries. Core EPUB mutation and real
historical-book gates are covered independently in the companion D41 suites.
"""
import copy
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from test_epub_fixture import minimal_epub_bytes


def polish_fixture_bytes(body="<p>他送來的禮物讓人覺得很窩心。</p>"):
    """A valid, synthetic fixture; never customer text or a paid API request."""
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(minimal_epub_bytes())) as source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "EPUB/chapter.xhtml":
                content = ("<html xmlns=\"http://www.w3.org/1999/xhtml\"><head>"
                           "<title>正文</title></head><body><h1 id=\"start\">正文</h1>"
                           + body + "</body></html>").encode()
            target.writestr(info, content)
    return output.getvalue()


class PrecisionContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epub-r3-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.uploads = self.root / "uploads"
        self.outputs = self.root / "outputs"
        self.uploads.mkdir()
        self.outputs.mkdir()
        self._patch(patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(self.root / "bootstrap.sqlite3"),
            "EPUB_PERSISTENT_STORE": "0", "SKIP_PAYMENT_CHECK": "0",
            "ADMIN_SECRET": "", "ALIPAY_APP_ID": "", "ALIPAY_DISABLE_PRECREATE": "0",
            "REPAIR_UPLOAD_DIR": str(self.root / "repair"),
            "CELERY_BROKER_URL": "", "REDIS_URL": "", "SENTRY_DSN": "",
            "SMTP_HOST": "", "NOTIFY_EMAIL_ENABLED": "0", "OWNER_PAYMENT_EMAIL_ENABLED": "0",
            "DEEPSEEK_API_KEY": "offline-test-never-use", "OPENAI_API_KEY": "offline-test-never-use",
            "DEEPSEEK_BASE_URL": "http://offline.invalid/v1", "OPENAI_BASE_URL": "http://offline.invalid/v1",
            "DEEPSEEK_MODEL": "deepseek-flash", "OPENAI_MODEL": "deepseek-flash",
            "EPUB_DEFAULT_TRANSLATION_MODEL": "deepseek-flash", "LLM_PRICING_FILE": "",
        }, clear=True))
        self._patch(patch("dotenv.load_dotenv", return_value=False))
        self.network = [self._patch(patch(target, side_effect=AssertionError("R3 forbids network")))
                        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo")]
        from app import main, job_runner
        from app.domain import payment_entitlement
        from app.models import ConversionResult, JobStatus
        from app.storage_db import Base, PersistentJobStore

        self.main = main
        self.runner = job_runner
        self.entitlement = payment_entitlement
        self.ConversionResult = ConversionResult
        self.JobStatus = JobStatus
        self.PersistentJobStore = PersistentJobStore
        self.engine = create_engine("sqlite:///" + str(self.root / "orders.sqlite3"),
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self._patch(patch.object(main, "job_store", self.store))
        self._patch(patch.object(job_runner, "job_store", self.store))
        self._patch(patch.object(main, "UPLOAD_DIR", self.uploads))
        self._patch(patch.object(main, "OUTPUT_DIR", self.outputs))
        self._patch(patch.object(job_runner, "OUTPUT_DIR", self.outputs))
        self._patch(patch("app.infra.execution_lease.tempfile.gettempdir", return_value=str(self.root)))
        self._patch(patch.object(main, "_use_celery", return_value=False))
        self.worker = self._patch(patch.object(main, "process_job"))
        # Keep authorized enqueue/outbox/attempt fencing real. Provider-boundary
        # tests below replace only the broker publisher; local test bypasses
        # are contained by the process_job mock, not by hiding the enqueue.
        self.enqueue = self._patch(patch.object(main, "_enqueue_conversion", wraps=main._enqueue_conversion))
        self.page_pay = self._patch(patch.object(main, "create_alipay_page_pay", return_value="https://example.invalid/pay"))
        self.qr_pay = self._patch(patch("app.infra.alipay.create_alipay_precreate", return_value="offline-qr"))
        self.notify = self._patch(patch.object(job_runner, "notify_job_completed"))
        self._patch(patch.object(job_runner, "report_error"))
        self.api = FastAPI()
        self.api.add_api_route("/jobs", main.create_job_v2, methods=["POST"])
        self.api.add_api_route("/jobs", main.list_jobs_v2, methods=["GET"])
        self.api.add_api_route("/jobs/{job_id}", main.get_job_v2, methods=["GET"])
        self.api.add_api_route("/jobs/{job_id}/download", main.download_result_v2, methods=["GET"])
        self.api.add_api_route("/estimate", main.estimate_polish_price, methods=["POST"])
        self.api.add_api_route("/webhook", main.alipay_webhook, methods=["POST"])
        self.client = self._patch_client(TestClient(self.api))

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _patch_client(self, client):
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    def create(self, *, body=None, filename="fixture.epub", content=None, **fields):
        data = {"output_mode": "simplified", "enable_precision_polish": "true", **fields}
        payload = content if content is not None else polish_fixture_bytes() if body is None else polish_fixture_bytes(body)
        return self.client.post("/jobs", files={"file": (filename, payload, "application/epub+zip")}, data=data)

    def job_from(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return self.store.get(response.json()["job_id"])

    def detail(self, job):
        response = self.client.get("/jobs/" + job.id, headers={"X-Job-Token": job.access_token})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def authorize(self, job):
        self.entitlement.grant_verified_entitlement(self.store, job, job.expected_amount, "verified_query")
        self.store.update_status(job.id, self.JobStatus.pending, "offline verified payment")
        return self.store.get(job.id)

    def assert_no_payment_or_execution(self):
        self.page_pay.assert_not_called()
        self.qr_pay.assert_not_called()
        self.worker.assert_not_called()
        self.enqueue.assert_not_called()
        self.assertEqual(self.store.list_jobs(), [])

    def test_unsupported_translation_combination_is_rejected_before_payment(self):
        response = self.create(enable_translation="true")
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_no_payment_or_execution()

    def test_traditional_output_is_rejected_before_payment(self):
        response = self.create(output_mode="traditional")
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_no_payment_or_execution()

    def test_non_epub_polish_is_rejected_before_payment(self):
        response = self.create(filename="book.md", content=b"# chapter\nordinary text")
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_no_payment_or_execution()

    def test_no_candidate_does_not_create_a_chargeable_addon(self):
        response = self.create(body="<p>這是一段普通的文字。</p>")
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_no_payment_or_execution()

    def test_risk_removed_by_real_base_conversion_is_not_billable(self):
        from app.engine.cleaners.cjk_normalizer import CjkNormalizer
        payload = polish_fixture_bytes("<p>我在超商買東西。</p>")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            raw = archive.read("EPUB/chapter.xhtml")
        converted = CjkNormalizer(output_mode="simplified").process(raw, 9).decode()
        self.assertIn("超商", raw.decode())
        self.assertIn("便利店", converted)
        self.assertNotIn("超", converted)
        with patch("app.engine.cleaners.llm_polish.LLMPolisher.__init__",
                   side_effect=AssertionError("Quotation must never construct a model")) as model:
            estimate = self.client.post("/estimate", files={"file": ("fixture.epub", payload, "application/epub+zip")})
            create = self.create(content=payload)
            self.assertEqual(estimate.status_code, 400, estimate.text)
            self.assertEqual(create.status_code, 400, create.text)
            model.assert_not_called()
        self.assert_no_payment_or_execution()

    def test_quote_and_create_follow_real_normalizer_when_general_lexicon_is_disabled(self):
        from app.engine.cleaners.cjk_normalizer import CjkNormalizer
        from lxml import etree
        payload = polish_fixture_bytes("<p>我在超商買東西。</p>")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            raw = archive.read("EPUB/chapter.xhtml")
        visible = "".join(etree.fromstring(raw).xpath("//*[local-name()='body']//text()"))
        original_chars = sum("\u4e00" <= char <= "\u9fff" for char in visible)
        with patch("app.engine.cleaners.llm_polish.LLMPolisher.__init__",
                   side_effect=AssertionError("Quotation must never construct a model")) as model:
            for domains in (["tech", "movie"], []):
                with self.subTest(lexicon_domains=domains):
                    # Independently run the actual conversion component; neither
                    # a raw-word guess nor the new inspection result is the oracle.
                    converted = CjkNormalizer(output_mode="simplified", lexicon_domains=domains).process(raw, 9).decode()
                    self.assertIn("超商", converted)
                    self.assertNotIn("便利店", converted)
                    fields = {"lexicon_domains_json": json.dumps(domains)}
                    estimate = self.client.post("/estimate", data=fields,
                        files={"file": ("fixture.epub", payload, "application/epub+zip")})
                    self.assertEqual(estimate.status_code, 200, estimate.text)
                    self.assertGreater(estimate.json()["candidates"], 0)
                    self.assertEqual(estimate.json()["char_count"], original_chars)
                    created = self.create(content=payload, **fields)
                    job = self.job_from(created)
                    self.assertEqual(job.status, self.JobStatus.pending_payment)
                    self.assertEqual(job.lexicon_domains, domains)
                    self.assertEqual(job.polish_char_count, original_chars)
                    self.assertEqual(job.translation_stats["precision_polish"]["candidates"], estimate.json()["candidates"])
                    self.assertEqual(job.translation_stats["precision_polish"]["quoted_amount"], estimate.json()["price_cny"])
            model.assert_not_called()
        self.assertEqual(self.qr_pay.call_count, 2)
        self.worker.assert_not_called()
        self.enqueue.assert_not_called()

    def test_empty_book_cannot_receive_minimum_polish_charge(self):
        response = self.create(body="")
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_no_payment_or_execution()

    def test_estimate_rejects_broken_epub_and_no_candidate(self):
        for payload in (b"this is not an epub", polish_fixture_bytes("<p>普通文字。</p>"), polish_fixture_bytes("")):
            with self.subTest(payload_size=len(payload)):
                response = self.client.post("/estimate", files={"file": ("fixture.epub", payload, "application/epub+zip")})
                self.assertEqual(response.status_code, 400, response.text)
        self.assert_no_payment_or_execution()

    def test_client_supplied_polish_order_cannot_replace_combined_order(self):
        response = self.create(polish_order_no="somebody-elses-paid-order")
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_no_payment_or_execution()

    def test_pending_quote_is_persistent_and_does_not_schedule_execution(self):
        response = self.create()
        job = self.job_from(response)
        self.assertEqual(job.status, self.JobStatus.pending_payment)
        self.assertTrue(job.enable_precision_polish)
        self.assertGreater(job.polish_char_count, 0)
        self.assertEqual(job.payment_entitlement["state"], "quoted")
        self.assertEqual(job.payment_entitlement["order_no"], job.id)
        self.assertEqual(job.payment_entitlement["amount"], job.expected_amount)
        self.assertEqual(Decimal(job.expected_amount), Decimal(response.json()["base_amount"]) +
                         Decimal(response.json()["precision_polish_amount"]))
        self.assertEqual(self.qr_pay.call_args.kwargs["out_trade_no"], job.id)
        self.assertEqual(self.qr_pay.call_args.kwargs["total_amount"], job.expected_amount)
        self.assertEqual(self.PersistentJobStore(self.engine).get(job.id).payment_entitlement, job.payment_entitlement)
        self.assertIsNotNone(self.detail(job)["precision_polish"])
        self.worker.assert_not_called()
        self.enqueue.assert_not_called()

    def test_frozen_addon_amount_cannot_be_repriced_by_execution_status(self):
        job = self.job_from(self.create())
        quoted = copy.deepcopy(job.payment_entitlement)
        self.assertEqual(quoted["product"], "conversion_precision_polish")
        self.assertEqual(quoted["precision_polish"]["char_count"], job.polish_char_count)
        self.assertEqual(quoted["precision_polish"]["amount"],
                         job.translation_stats["precision_polish"]["quoted_amount"])
        self.assertNotEqual(self.entitlement.precision_polish_entitlement_reason(job), "")
        job = self.authorize(job)
        self.assertEqual(self.entitlement.precision_polish_entitlement_reason(job), "")
        frozen = copy.deepcopy(job.payment_entitlement)
        altered = copy.deepcopy(job.translation_stats)
        altered["precision_polish"]["quoted_amount"] = "0.01"
        self.store.update_status(job.id, self.JobStatus.running, "offline mutation", translation_stats=altered)
        changed = self.store.get(job.id)
        self.assertEqual(changed.payment_entitlement, frozen)
        self.assertNotEqual(self.entitlement.precision_polish_entitlement_reason(changed), "")
        self.entitlement.grant_verified_entitlement(self.store, changed, job.expected_amount, "verified_webhook")
        self.assertEqual(self.store.get(job.id).payment_entitlement, frozen)

    def test_wrong_amount_or_browser_source_cannot_authorize_addon(self):
        job = self.job_from(self.create())
        for amount, source in (("0.01", "verified_query"), (job.expected_amount, "browser")):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.entitlement.grant_verified_entitlement(self.store, job, amount, source)
        self.assertEqual(self.store.get(job.id).payment_entitlement["state"], "quoted")

    @staticmethod
    def verified_webhook_payload(job, **overrides):
        return {"out_trade_no": job.id, "total_amount": job.expected_amount,
                "trade_status": "TRADE_SUCCESS", "app_id": "offline-app",
                "seller_id": "offline-seller", "sign": "provider-boundary-mock-only", **overrides}

    def test_real_conversion_webhook_authorizes_frozen_precision_once(self):
        job = self.job_from(self.create())
        frozen = copy.deepcopy(job.payment_entitlement)
        from app.storage_db import OrderEventRecord
        with patch.dict(os.environ, {"ALIPAY_APP_ID": "offline-app", "ALIPAY_SELLER_ID": "offline-seller"}), \
             patch.object(self.main, "verify_alipay_notification", return_value=True) as verify, \
             patch.object(self.main, "_use_celery", return_value=True), \
             patch("app.infra.job_dispatch_publisher.publish_conversion") as dispatch, \
             patch("app.domain.payment_email_service.queue_paid_order_email") as email:
            for _ in range(2):
                response = self.client.post("/webhook", data=self.verified_webhook_payload(job))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.text, "success")
            self.assertEqual(verify.call_count, 2)
            dispatch.assert_called_once_with(job.id, job.translation_stats["attempt_id"])
            email.assert_called_once()
            self.assertEqual(email.call_args.args[2], "conversion")
        paid = self.store.get(job.id)
        self.assertEqual(paid.status, self.JobStatus.pending)
        self.assertEqual(paid.payment_entitlement["state"], "paid")
        self.assertEqual(paid.payment_entitlement["source"], "verified_webhook")
        self.assertEqual(paid.payment_entitlement["precision_polish"], frozen["precision_polish"])
        self.assertEqual(self.entitlement.precision_polish_entitlement_reason(paid), "")
        with self.store._Session() as session:
            event = session.get(OrderEventRecord, (job.id, "payment_succeeded"))
            self.assertIsNotNone(event)
            self.assertEqual(event.source, "verified_webhook")

    def test_conversion_webhook_rejects_wrong_amount_identity_or_signature(self):
        job = self.job_from(self.create())
        original = copy.deepcopy(job.payment_entitlement)
        with patch.dict(os.environ, {"ALIPAY_APP_ID": "offline-app", "ALIPAY_SELLER_ID": "offline-seller"}), \
             patch.object(self.main, "verify_alipay_notification", return_value=True) as verify, \
             patch.object(self.main, "_use_celery", return_value=True), \
             patch("app.infra.job_dispatch_publisher.publish_conversion") as dispatch, \
             patch("app.domain.payment_email_service.queue_paid_order_email") as email:
            for override in ({"total_amount": "0.01"}, {"app_id": "wrong-app"}, {"seller_id": "wrong-seller"}):
                with self.subTest(override=override):
                    response = self.client.post("/webhook", data=self.verified_webhook_payload(job, **override))
                    self.assertEqual(response.text, "fail")
            verify.return_value = False
            self.assertEqual(self.client.post("/webhook", data=self.verified_webhook_payload(job)).text, "fail")
            dispatch.assert_not_called()
            email.assert_not_called()
        unpaid = self.store.get(job.id)
        self.assertEqual(unpaid.status, self.JobStatus.pending_payment)
        self.assertEqual(unpaid.payment_entitlement, original)

    def test_client_cannot_promote_quote_to_server_test_authorization(self):
        response = self.create(is_test_order="true", SKIP_PAYMENT_CHECK="1",
                               payment_entitlement='{"state":"paid"}', status="success")
        job = self.job_from(response)
        self.assertFalse(job.is_test_order)
        self.assertEqual(job.status, self.JobStatus.pending_payment)
        self.assertEqual(job.payment_entitlement["state"], "quoted")
        self.worker.assert_not_called()
        self.enqueue.assert_not_called()

    def test_explicit_server_test_config_is_distinct_from_paid_entitlement(self):
        with patch.dict(os.environ, {"SKIP_PAYMENT_CHECK": "1"}):
            job = self.job_from(self.create())
        self.assertTrue(job.is_test_order)
        self.assertEqual(job.payment_entitlement["state"], "test_authorized")
        self.assertEqual(job.payment_entitlement["source"], "server_test_bypass")
        self.assertEqual(self.entitlement.precision_polish_entitlement_reason(job), "")
        self.page_pay.assert_not_called()
        self.qr_pay.assert_not_called()

    def test_authoritative_stats_survive_store_reopen_and_api_refresh(self):
        job = self.authorize(self.job_from(self.create()))
        frozen = copy.deepcopy(job.payment_entitlement)
        for precision_status in ("pending", "running", "completed", "no_candidates", "failed"):
            with self.subTest(precision_status=precision_status):
                snapshot = copy.deepcopy(job.translation_stats)
                snapshot["precision_polish"].update(status=precision_status, paragraphs_sent=2,
                                                    paragraphs_polished=1, marker="offline-persistent")
                self.store.update_status(job.id, self.JobStatus.running, "offline status", translation_stats=snapshot)
                reopened = self.PersistentJobStore(self.engine)
                reloaded = reopened.get(job.id)
                self.assertEqual(reloaded.translation_stats["precision_polish"], snapshot["precision_polish"])
                self.assertEqual(reloaded.payment_entitlement, frozen)
                with patch.object(self.main, "job_store", reopened):
                    self.assertEqual(self.detail(reloaded)["precision_polish"], snapshot["precision_polish"])
                    listing = self.client.get("/jobs")
                    self.assertEqual(listing.status_code, 200, listing.text)
                    row = next(item for item in listing.json()["items"] if item["job_id"] == job.id)
                    self.assertEqual(row["precision_polish"], snapshot["precision_polish"])

    def test_verified_failed_addon_keeps_purchase_terms_through_existing_admin_retry(self):
        job = self.authorize(self.job_from(self.create()))
        frozen = copy.deepcopy(job.payment_entitlement)
        previous = copy.deepcopy(job.translation_stats)
        previous["precision_polish"].update(status="failed", reviewed=1, changed=1, api_calls=2,
                                            failed=1, refund_required=True, reason="provider_error")
        self.store.update_status(job.id, self.JobStatus.failed, "offline precision failure", translation_stats=previous)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                text("SELECT precision_polish_status FROM epub_jobs WHERE id=:id"),
                {"id": job.id}).scalar_one(), "failed")
        restarted, reason = self.store.restart_translation_attempt(
            job.id, attempt_id="offline-admin-retry", action_label="管理员重试", max_free_retries=-1,
            started_at=datetime.now(timezone.utc), failed_only=True)
        self.assertEqual(reason, "ok")
        self.assertEqual(restarted.payment_entitlement, frozen)
        precision = restarted.translation_stats["precision_polish"]
        for key in ("quoted_amount", "char_count", "order_no"):
            self.assertEqual(precision[key], previous["precision_polish"][key])
        self.assertEqual(precision["status"], "pending")
        self.assertEqual(precision.get("reviewed", 0), 0)
        self.assertEqual(precision.get("changed", 0), 0)
        self.assertEqual(precision.get("api_calls", 0), 0)
        self.assertEqual(self.entitlement.precision_polish_entitlement_reason(restarted), "")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                text("SELECT precision_polish_status FROM epub_jobs WHERE id=:id"),
                {"id": job.id}).scalar_one(), "pending")

    @staticmethod
    def service_stats(**overrides):
        return {"version": 1, "status": "completed", "model": "deepseek-flash",
                "provider": "offline.invalid", "documents_scanned": 1, "paragraphs_scanned": 1,
                "candidates": 1, "reviewed": 1, "changed": 1, "unchanged": 0,
                "api_calls": 1, "retries": 0, "failed": 0, "reason": "",
                "refund_required": False, "validation_passed": True, **overrides}

    def record_model_usage(self):
        from app.infra.llm_usage_ledger import accounted_call
        return accounted_call(lambda: {
            "id": "offline-request", "model": "deepseek-flash",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                      "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100},
        }, model="deepseek-flash", base_url="https://api.deepseek.com/v1", stage="precision_polish")

    def execute(self, job, *, service_effect=None, valid_conversion=True):
        from app.domain import precision_polish_service
        self.execution_order = []
        self.converted_bytes = polish_fixture_bytes("<p>基础转换结果：窝心。</p>")
        self.polished_bytes = polish_fixture_bytes("<p>精校结果：暖心。</p>")

        def convert(source, destination, *_args, **_kwargs):
            self.execution_order.append("conversion")
            self.assertEqual(Path(source), Path(job.input_path))
            Path(destination).write_bytes(self.converted_bytes)
            return self.ConversionResult(validation_passed=valid_conversion,
                                         error_code=None if valid_conversion else "EPUB_VALIDATION_FAILED")

        def polish(source, destination, **kwargs):
            self.execution_order.append("precision_polish")
            self.assertEqual(Path(source).read_bytes(), self.converted_bytes)
            self.assertNotEqual(Path(source), Path(job.input_path))
            if service_effect is not None:
                return service_effect(source, destination, **kwargs)
            self.record_model_usage()
            Path(destination).write_bytes(self.polished_bytes)
            return self.service_stats()

        with ExitStack() as stack:
            convert_mock = stack.enter_context(patch.object(self.runner.converter, "convert_file_to_horizontal", side_effect=convert))
            polish_mock = stack.enter_context(patch.object(precision_polish_service, "run_precision_polish", side_effect=polish))
            # Support either ordinary import style without replacing the runner
            # itself or hiding its actual sequence/authorization decisions.
            if hasattr(self.runner, "run_precision_polish"):
                stack.enter_context(patch.object(self.runner, "run_precision_polish", polish_mock))
            self.runner.run_job(job.id)
        return self.store.get(job.id), convert_mock, polish_mock

    def test_paid_execution_runs_after_conversion_and_preserves_quote_and_billing(self):
        job = self.authorize(self.job_from(self.create()))
        frozen = copy.deepcopy(job.payment_entitlement)
        finished, converter, polisher = self.execute(job)
        self.assertEqual(self.execution_order, ["conversion", "precision_polish"])
        converter.assert_called_once()
        polisher.assert_called_once()
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)
        self.assertEqual(Path(finished.output_path).read_bytes(), self.polished_bytes)
        self.assertEqual(finished.payment_entitlement, frozen)
        self.assertEqual(finished.translation_stats["precision_polish"]["status"], "completed")
        self.assertEqual(finished.translation_stats["precision_polish"]["quoted_amount"],
                         frozen["precision_polish"]["amount"])
        self.assertEqual(self.entitlement.precision_polish_entitlement_reason(finished), "")
        detail = self.detail(finished)
        self.assertIsNotNone(detail["download_url"])
        self.assertEqual(detail["precision_polish"]["reviewed"], 1)
        self.assertEqual(detail["translation_stats"]["billing"]["requests"], 1)
        self.assertEqual(detail["translation_stats"]["billing"]["stages"]["precision_polish"]["total_tokens"], 120)
        self.assertTrue(any("precision" in stage.stage_name for stage in self.store.list_stages(job.id)))
        download = self.client.get("/jobs/" + job.id + "/download", headers={"X-Job-Token": job.access_token})
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.content, self.polished_bytes)

    def test_running_precision_progress_is_persisted_before_service_returns(self):
        job = self.authorize(self.job_from(self.create()))
        def effect(_source, destination, **kwargs):
            callback = kwargs["stats_callback"]
            self.assertTrue(callable(callback))
            self.assertTrue(callable(kwargs["cancel_check"]))
            progress = self.service_stats(status="running", reviewed=0, changed=0, api_calls=0,
                                          validation_passed=False)
            callback(progress)
            reloaded = self.PersistentJobStore(self.engine).get(job.id)
            self.assertEqual(reloaded.status, self.JobStatus.running)
            self.assertEqual(reloaded.translation_stats["precision_polish"]["status"], "running")
            self.assertEqual(self.detail(reloaded)["precision_polish"]["reviewed"], 0)
            self.assertIsNone(self.detail(reloaded)["download_url"])
            Path(destination).write_bytes(self.polished_bytes)
            return self.service_stats()
        finished, _, polisher = self.execute(job, service_effect=effect)
        polisher.assert_called_once()
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)

    def test_polish_failure_does_not_deliver_base_conversion_or_claim_refund(self):
        from app.domain.precision_polish_service import PrecisionPolishError
        job = self.authorize(self.job_from(self.create()))
        def effect(_source, _destination, **_kwargs):
            self.record_model_usage()
            raise PrecisionPolishError("provider_unavailable", "offline provider failure", self.service_stats(
                status="failed", reviewed=0, changed=0, failed=1, reason="provider_unavailable",
                refund_required=True, validation_passed=False))
        failed, _, polisher = self.execute(job, service_effect=effect)
        polisher.assert_called_once()
        self.assertEqual(failed.status, self.JobStatus.failed)
        self.assertEqual(failed.error_code, "PRECISION_POLISH_FAILED")
        self.assertFalse(failed.output_path)
        detail = self.detail(failed)
        self.assertIsNone(detail["download_url"])
        self.assertEqual(detail["precision_polish"]["status"], "failed")
        self.assertEqual(detail["precision_polish"]["reason"], "provider_unavailable")
        self.assertTrue(detail["precision_polish"]["refund_required"])
        self.assertNotEqual(detail["precision_polish"].get("refund_status"), "refunded")
        self.assertNotIn("已退款", detail["message"])
        self.assertEqual(detail["translation_stats"]["billing"]["requests"], 1)
        download = self.client.get("/jobs/" + job.id + "/download", headers={"X-Job-Token": job.access_token})
        self.assertEqual(download.status_code, 400, download.text)

    def test_governor_refusal_preserves_audit_flags_and_refund_contract(self):
        from app.infra.llm_gateway import GatewayControlError
        job = self.authorize(self.job_from(self.create()))
        def refuse(*_args, **_kwargs):
            raise GatewayControlError('controlled quota refusal')
        failed, _, polisher = self.execute(job, service_effect=refuse)
        self.assertEqual(polisher.call_count, 1)
        self.assertEqual(failed.status, self.JobStatus.failed)
        self.assertEqual(failed.error_code, 'PRECISION_POLISH_FAILED')
        self.assertTrue(failed.translation_stats['model_governor_blocked'])
        self.assertFalse(failed.translation_stats['deliverable'])
        self.assertTrue(failed.translation_stats['precision_polish']['refund_required'])
        self.assertIsNone(self.detail(failed)['download_url'])

    def test_failed_base_validation_never_invokes_paid_polisher(self):
        job = self.authorize(self.job_from(self.create()))
        failed, converter, polisher = self.execute(job, valid_conversion=False)
        converter.assert_called_once()
        polisher.assert_not_called()
        self.assertEqual(failed.status, self.JobStatus.failed)
        self.assertIsNone(self.detail(failed)["download_url"])

    def test_disabled_addon_has_no_precision_execution(self):
        job = self.job_from(self.create(enable_precision_polish="false"))
        self.store.update_status(job.id, self.JobStatus.pending, "offline base conversion payment")
        finished, converter, polisher = self.execute(self.store.get(job.id))
        converter.assert_called_once()
        polisher.assert_not_called()
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)
        self.assertEqual(Path(finished.output_path).read_bytes(), self.converted_bytes)
        self.assertFalse(finished.enable_precision_polish)

    def test_mutable_queued_status_alone_cannot_authorize_paid_polish(self):
        job = self.job_from(self.create())
        self.store.update_status(job.id, self.JobStatus.pending, "not verified: simulate old execution state")
        failed, _converter, polisher = self.execute(self.store.get(job.id))
        polisher.assert_not_called()
        self.assertEqual(failed.status, self.JobStatus.failed)
        self.assertIsNone(self.detail(failed)["download_url"])
        self.assertEqual(failed.payment_entitlement["state"], "quoted")

    def test_duplicate_executor_cannot_issue_a_second_precision_call(self):
        job = self.authorize(self.job_from(self.create()))
        def effect(_source, destination, **_kwargs):
            self.runner.run_job(job.id)  # Real local lease is still held.
            Path(destination).write_bytes(self.polished_bytes)
            return self.service_stats()
        finished, converter, polisher = self.execute(job, service_effect=effect)
        converter.assert_called_once()
        polisher.assert_called_once()
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)

    def test_legacy_missing_attempt_keeps_same_execution_lease_after_initialization(self):
        job = self.authorize(self.job_from(self.create()))
        legacy_stats = copy.deepcopy(job.translation_stats)
        legacy_stats.pop("attempt_id", None)
        # update_status intentionally merges stats; construct the historical
        # missing-key shape only in this test's isolated SQLite database.
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE epub_jobs SET translation_stats_json=:stats WHERE id=:id"),
                               {"stats": json.dumps(legacy_stats), "id": job.id})
        job = self.store.get(job.id)
        self.assertNotIn("attempt_id", job.translation_stats)
        def effect(_source, destination, **_kwargs):
            self.runner.run_job(job.id)
            Path(destination).write_bytes(self.polished_bytes)
            return self.service_stats()
        finished, converter, polisher = self.execute(job, service_effect=effect)
        converter.assert_called_once()
        polisher.assert_called_once()
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)
        self.assertTrue(finished.translation_stats.get("attempt_id"))

    def test_paid_no_candidates_is_explicit_and_cannot_claim_addon_completion(self):
        for service_status, validation_passed, expected_status, expected_reason in (
                ("no_candidates", True, "no_candidates", "no_candidates"),
                ("completed", False, "failed", "incomplete_review")):
            with self.subTest(service_status=service_status, validation_passed=validation_passed):
                job = self.authorize(self.job_from(self.create()))
                def effect(source, destination, **_kwargs):
                    if Path(source) != Path(destination):
                        shutil.copyfile(source, destination)
                    return self.service_stats(status=service_status, candidates=0, reviewed=0,
                                              changed=0, api_calls=0, validation_passed=validation_passed)
                finished, _, polisher = self.execute(job, service_effect=effect)
                polisher.assert_called_once()
                self.assertEqual(finished.status, self.JobStatus.failed, finished.message)
                self.assertEqual(finished.error_code, "PRECISION_POLISH_FAILED")
                self.assertFalse(finished.output_path)
                detail = self.detail(finished)
                self.assertIsNone(detail["download_url"])
                precision = detail["precision_polish"]
                self.assertEqual(precision["status"], expected_status)
                self.assertEqual(precision["reason"], expected_reason)
                self.assertEqual(precision["api_calls"], 0)
                self.assertEqual(precision["reviewed"], 0)
                self.assertTrue(precision["refund_required"])


if __name__ == "__main__":
    unittest.main()
