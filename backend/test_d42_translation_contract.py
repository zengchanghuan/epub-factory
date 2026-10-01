"""R4 HTTP/runner contracts with actual adapters and isolated, offline stores.

The fast translation executor and preflight model boundary are substitutes; the
HTTP routes, EPUB/DOCX/Markdown normalization, prices, frozen plan, persistence,
execution lease, cancellation and final delivery audit remain real. These tests
do not claim provider translation quality or historical-document coverage.
"""
import copy
import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from test_epub_fixture import minimal_epub_bytes


SOURCE_TEXT = "Alice reads an original book about liberty and careful reasoning."


def epub_bytes(text=SOURCE_TEXT, *, translated=False):
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(minimal_epub_bytes())) as source, zipfile.ZipFile(result, "w") as target:
        for info in source.infolist():
            value = source.read(info.filename)
            if info.filename == "EPUB/chapter.xhtml":
                value = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title>'
                         '</head><body><h1 id="start">Chapter</h1><p>' + escape(text) + '</p></body></html>').encode()
            if translated and info.filename.endswith((".opf", ".xhtml")):
                value = value.decode().replace("Offline fixture", "离线书稿").replace("Chapter", "章节").replace("Contents", "目录").encode()
            target.writestr(info, value)
    return result.getvalue()


def docx_bytes(text=SOURCE_TEXT):
    """Minimal real Word package consumed by mammoth, not a mocked adapter."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>' + escape(text) + '</w:t></w:r></w:p></w:body></w:document>')
    return stream.getvalue()


class TranslationContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epub-r4-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.uploads, self.outputs = self.root / "uploads", self.root / "outputs"
        self.uploads.mkdir()
        self.outputs.mkdir()
        self._patch(patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(self.root / "bootstrap.sqlite3"),
            "EPUB_PERSISTENT_STORE": "0", "SKIP_PAYMENT_CHECK": "0", "EPUB_FAST_TRANSLATION": "1",
            "ADMIN_SECRET": "", "ALIPAY_APP_ID": "", "ALIPAY_DISABLE_PRECREATE": "0",
            "REPAIR_UPLOAD_DIR": str(self.root / "repair"), "CELERY_BROKER_URL": "", "REDIS_URL": "",
            "SENTRY_DSN": "", "SMTP_HOST": "", "NOTIFY_EMAIL_ENABLED": "0", "OWNER_PAYMENT_EMAIL_ENABLED": "0",
            "DEEPSEEK_API_KEY": "offline-test-never-use", "OPENAI_API_KEY": "offline-test-never-use",
            "DEEPSEEK_BASE_URL": "http://offline.invalid/v1", "OPENAI_BASE_URL": "http://offline.invalid/v1",
            "OPENAI_MODEL": "deepseek-flash", "EPUB_DEFAULT_TRANSLATION_MODEL": "deepseek-flash",
            "EPUB_TRANSLATION_CHECKPOINT_DB": str(self.root / "checkpoints.sqlite3"), "LLM_PRICING_FILE": "",
        }, clear=True))
        self._patch(patch("dotenv.load_dotenv", return_value=False))
        self.network = [self._patch(patch(target, side_effect=AssertionError("R4 forbids network")))
                        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo")]
        from app import main, job_runner
        from app.domain import payment_entitlement
        from app.engine.translation_cache import TranslationCache
        from app.models import ConversionResult, JobStatus
        from app.storage_db import Base, PersistentJobStore

        self.main, self.runner, self.entitlement = main, job_runner, payment_entitlement
        self.ConversionResult, self.JobStatus, self.PersistentJobStore = ConversionResult, JobStatus, PersistentJobStore
        self.engine = create_engine("sqlite:///" + str(self.root / "orders.sqlite3"),
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.cache = TranslationCache(str(self.root / "cache.sqlite3"))
        self._patch(patch("app.engine.translation_cache.TranslationCache", return_value=self.cache))
        self._patch(patch.object(main, "job_store", self.store))
        self._patch(patch.object(job_runner, "job_store", self.store))
        self._patch(patch.object(main, "UPLOAD_DIR", self.uploads))
        self._patch(patch.object(main, "OUTPUT_DIR", self.outputs))
        self._patch(patch.object(job_runner, "OUTPUT_DIR", self.outputs))
        self._patch(patch("app.infra.execution_lease.tempfile.gettempdir", return_value=str(self.root)))
        self._patch(patch.object(main, "_use_celery", return_value=False))
        self.worker = self._patch(patch.object(main, "process_job"))
        self.enqueue = self._patch(patch.object(main, "_enqueue_conversion"))
        self.page_pay = self._patch(patch.object(main, "create_alipay_page_pay", return_value="https://example.invalid/pay"))
        self.qr_pay = self._patch(patch("app.infra.alipay.create_alipay_precreate", return_value="offline-qr"))
        self._patch(patch.object(job_runner, "notify_job_completed"))
        self._patch(patch.object(job_runner, "report_error"))
        self.preflight_paths = []
        self.preflight = self._patch(patch.object(main, "build_translation_preflight", side_effect=self.offline_preflight))
        self.api = FastAPI()
        self.api.add_api_route("/jobs", main.create_job_v2, methods=["POST"])
        self.api.add_api_route("/jobs/{job_id}", main.get_job_v2, methods=["GET"])
        self.api.add_api_route("/jobs/{job_id}/confirm", main.confirm_translation_profile_v2, methods=["POST"])
        self.api.add_api_route("/jobs/{job_id}/download", main.download_result_v2, methods=["GET"])
        self.client = TestClient(self.api)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    def payload(self, extension):
        # ZIP writers embed the current time. Compare with the exact bytes
        # uploaded by this test, not a second ZIP generated across a clock tick.
        if not hasattr(self, "_source_payloads"):
            self._source_payloads = {}
        if extension not in self._source_payloads:
            self._source_payloads[extension] = (epub_bytes() if extension == ".epub" else
                docx_bytes() if extension == ".docx" else ("# Chapter\n\n" + SOURCE_TEXT + "\n").encode())
        return self._source_payloads[extension]

    def offline_preflight(self, *, epub_path, job_id, **kwargs):
        from app.domain.manifest_service import build_manifest
        path = Path(epub_path)
        self.assertEqual(path.suffix.lower(), ".epub")
        self.assertTrue(zipfile.is_zipfile(path))
        manifest = build_manifest(str(path), job_id)
        self.assertFalse(manifest.get("error"), manifest)
        chapters = [{"chapter_id": row["chapter_id"], "file_path": row["file_path"],
                     "strategy": "neutral_faithful"}
                    for row in manifest["chapters"] if row.get("chapter_kind") == "body"]
        self.assertTrue(chapters)
        self.preflight_paths.append(path)
        return {"version": 1, "confirmed": False, "status": "ready", "profile": {"genre": "fiction"},
                "resolved_strategy": "neutral_faithful", "glossary": dict(kwargs.get("user_glossary") or {}),
                "glossary_catalog": [], "characters": [], "chapters": chapters,
                "chapter_strategy_overrides": {}, "source_warnings": []}

    def create(self, extension=".epub", *, payload=None, **fields):
        data = {"output_mode": "simplified", "enable_translation": "true", "profile_confirmation": "true",
                "translation_model": "deepseek-v4-pro", "translation_quality": "literary", "cache_policy": "fresh",
                "translation_strategy": "literary_narrative", "temperature": "0.17", "device": "apple",
                "traditional_variant": "tw", "lexicon_domains_json": '["tech"]', "enable_proper_noun": "false",
                "glossary_json": '{"liberty":"自由"}', **fields}
        return self.client.post("/jobs", files={"file": ("original" + extension,
            self.payload(extension) if payload is None else payload, "application/octet-stream")}, data=data)

    def job_from(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return self.store.get(response.json()["job_id"])

    def confirm(self, job, **overrides):
        preflight = job.translation_stats["translation_preflight"]
        chapter = preflight["chapters"][0]["chapter_id"]
        return self.client.post("/jobs/" + job.id + "/confirm", headers={"X-Job-Token": job.access_token}, json={
            "translation_strategy": "academic_rigorous", "bilingual": True, "glossary": {"liberty": "自由"},
            "characters": [{"source_name": "Alice", "translated_name": "爱丽丝", "pronouns": "she"}],
            "chapter_strategy_overrides": {chapter: "literary_narrative"}, "enable_term_highlights": True,
            **overrides})

    def authorize(self, job):
        self.entitlement.grant_verified_entitlement(self.store, job, job.expected_amount, "verified_query")
        self.store.update_status(job.id, self.JobStatus.pending, "offline verified payment")
        return self.store.get(job.id)

    def confirmed_paid_job(self, extension=".epub"):
        job = self.job_from(self.create(extension))
        response = self.confirm(job)
        self.assertEqual(response.status_code, 200, response.text)
        return self.authorize(self.store.get(job.id))

    def assert_no_order_or_payment(self):
        self.assertEqual(self.store.list_jobs(), [])
        self.page_pay.assert_not_called()
        self.qr_pay.assert_not_called()
        self.worker.assert_not_called()
        self.enqueue.assert_not_called()

    def test_all_formats_are_priced_from_actual_body_before_confirmation(self):
        for extension in (".epub", ".docx", ".md", ".markdown"):
            with self.subTest(extension=extension):
                response = self.create(extension)
                job = self.job_from(response)
                self.assertEqual(job.status, self.JobStatus.awaiting_confirmation)
                self.assertGreaterEqual(response.json()["estimated_chars"], len(SOURCE_TEXT))
                self.assertEqual(Path(job.input_path).suffix, extension)
                self.assertEqual(Path(job.input_path).read_bytes(), self.payload(extension))
                self.assertEqual(job.payment_entitlement["translation_quality"], "literary")
                self.assertEqual(job.payment_entitlement["translation_model"], "deepseek-v4-pro")
                self.assertEqual(job.payment_entitlement["state"], "quoted")
                self.assertEqual(job.translation_stats["translation_input"]["source_sha256"],
                                 hashlib.sha256(Path(job.input_path).read_bytes()).hexdigest())
                if extension != ".epub":
                    self.assertFalse(self.preflight_paths[-1].exists(), "temporary normalized source leaked")
        self.assertEqual(self.preflight.call_count, 4)
        self.page_pay.assert_not_called()
        self.worker.assert_not_called()

    def test_confirmation_freezes_same_chapter_plan_for_docx_and_markdown(self):
        for extension in (".docx", ".md"):
            with self.subTest(extension=extension):
                job = self.job_from(self.create(extension))
                original_plan = copy.deepcopy(job.payment_entitlement)
                original_chapters = copy.deepcopy(job.translation_stats["translation_preflight"]["chapters"])
                response = self.confirm(job)
                self.assertEqual(response.status_code, 200, response.text)
                saved = self.PersistentJobStore(self.engine).get(job.id)
                self.assertEqual(saved.status, self.JobStatus.pending_payment)
                self.assertEqual(saved.payment_entitlement, original_plan)
                self.assertEqual(saved.translation_strategy, "academic_rigorous")
                self.assertEqual(saved.translation_stats["translation_preflight"]["chapters"], original_chapters)
                self.assertTrue(saved.translation_stats["translation_preflight"]["confirmed"])
                self.assertEqual(saved.glossary["Alice"], "爱丽丝")
                self.assertTrue(saved.bilingual)
                self.assertEqual(self.page_pay.call_args.kwargs["total_amount"], job.expected_amount)

    def test_direct_payment_without_confirmation_still_uses_actual_text_count(self):
        for extension in (".docx", ".md"):
            with self.subTest(extension=extension):
                response = self.create(extension, profile_confirmation="false")
                job = self.job_from(response)
                self.assertEqual(job.status, self.JobStatus.pending_payment)
                self.assertGreaterEqual(response.json()["estimated_chars"], len(SOURCE_TEXT))
        self.preflight.assert_not_called()
        self.assertEqual(self.page_pay.call_count, 2)

    def test_quality_defaults_and_flash_aliases_survive_normalized_execution(self):
        for quality, alias, cache in (("standard", "deepseek-flash", "reuse"),
                                      ("high", "deepseek-v4-flash", "verified"),
                                      ("literary", "deepseek-v4-flash-vision-exp", "verified")):
            with self.subTest(quality=quality, alias=alias):
                job = self.job_from(self.create(".md", profile_confirmation="false", translation_model=alias,
                                                translation_quality=quality, cache_policy=""))
                self.assertEqual(job.cache_policy, cache)
                self.assertEqual(job.payment_entitlement["translation_model"], "deepseek-flash")
                self.assertEqual(job.payment_entitlement["translation_quality"], quality)
                paid = self.authorize(job)
                finished, executor, converter = self.execute(paid)
                self.assertEqual(finished.status, self.JobStatus.success, finished.message)
                plan = executor.call_args.kwargs["job"]
                self.assertEqual(plan.translation_model, alias)
                self.assertEqual(plan.translation_quality, quality)
                self.assertEqual(plan.cache_policy, cache)
                converter.assert_not_called()

    def test_zero_character_pricing_fallback_cannot_reach_profile_or_payment(self):
        with patch.object(self.main, "_estimate_translation_pricing", return_value={"total_chars": 0, "price_cny": "0.01"}):
            response = self.create(".md")
        self.assertEqual(response.status_code, 400, response.text)
        self.preflight.assert_not_called()
        self.assert_no_order_or_payment()

    def test_pdf_extension_and_disguised_pdf_are_rejected_without_side_effects(self):
        for extension in (".pdf", ".md", ".docx"):
            with self.subTest(extension=extension):
                response = self.create(extension, payload=b"%PDF-1.7\nnot supported")
                self.assertEqual(response.status_code, 400, response.text)
        self.preflight.assert_not_called()
        self.assert_no_order_or_payment()

    def test_broken_docx_or_empty_markdown_cannot_be_minimum_price_orders(self):
        for extension, payload in ((".docx", b"not a ZIP"), (".md", b"\n\n")):
            for confirmation in ("true", "false"):
                with self.subTest(extension=extension, confirmation=confirmation):
                    response = self.create(extension, payload=payload, profile_confirmation=confirmation)
                    self.assertEqual(response.status_code, 400, response.text)
        self.preflight.assert_not_called()
        self.assert_no_order_or_payment()

    def test_translation_kill_switch_rejects_before_profile_payment_or_persist(self):
        with patch.dict(os.environ, {"EPUB_FAST_TRANSLATION": "0"}):
            for extension in (".epub", ".docx", ".md"):
                with self.subTest(extension=extension):
                    response = self.create(extension)
                    self.assertEqual(response.status_code, 503, response.text)
        self.preflight.assert_not_called()
        self.assert_no_order_or_payment()

    def test_confirmation_rechecks_kill_switch_without_claiming_or_payment(self):
        job = self.job_from(self.create(".md"))
        original = copy.deepcopy(job.translation_stats)
        with patch.dict(os.environ, {"EPUB_FAST_TRANSLATION": "0"}):
            response = self.confirm(job)
        self.assertEqual(response.status_code, 503, response.text)
        unchanged = self.store.get(job.id)
        self.assertEqual(unchanged.status, self.JobStatus.awaiting_confirmation)
        self.assertEqual(unchanged.translation_stats, original)
        self.page_pay.assert_not_called()

    def test_confirmation_revalidates_changed_docx_before_payment(self):
        job = self.job_from(self.create(".docx"))
        Path(job.input_path).write_bytes(b"damaged after analysis")
        response = self.confirm(job)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.awaiting_confirmation)
        self.page_pay.assert_not_called()

    def test_valid_but_changed_source_cannot_use_original_confirmed_quote(self):
        job = self.job_from(self.create(".md"))
        Path(job.input_path).write_text("# Different book\n\nA different source has replaced the quoted manuscript.\n")
        response = self.confirm(job)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.awaiting_confirmation)
        self.page_pay.assert_not_called()

    def execute(self, job, effect=None, *, expected_attempt_id=None):
        from app.domain import fast_translation_runner
        self.output_bytes = epub_bytes("爱丽丝阅读原著，思考自由与严谨推理。", translated=True)
        self.executor_paths = []

        def fast(**kwargs):
            self.executor_paths.append(Path(kwargs["input_path"]))
            if effect:
                return effect(**kwargs)
            Path(kwargs["output_path"]).write_bytes(self.output_bytes)
            return self.ConversionResult(translation_stats={"translated_chunks": 1, "failed_chunks": 0,
                "api_calls": 1, "translation_quality": job.translation_quality, "cache_policy": job.cache_policy})

        with patch.object(fast_translation_runner, "run_fast_translation_job", side_effect=fast) as executor, \
             patch.object(self.runner.converter, "convert_file_to_horizontal",
                          side_effect=AssertionError("translation may not silently use the old converter")) as old_converter:
            self.runner.run_job(job.id, expected_attempt_id)
        return self.store.get(job.id), executor, old_converter

    def test_paid_formats_reach_one_executor_with_full_immutable_job_plan(self):
        from app.domain.manifest_service import build_manifest
        for extension in (".epub", ".docx", ".md"):
            with self.subTest(extension=extension):
                job = self.confirmed_paid_job(extension)
                before = copy.deepcopy(job)
                seen = []

                def capture(**kwargs):
                    plan = kwargs["job"]
                    for key in ("id", "source_filename", "input_path", "output_mode", "traditional_variant",
                                "translation_model", "translation_quality", "cache_policy", "translation_strategy",
                                "target_lang", "bilingual", "glossary", "temperature", "device", "lexicon_domains",
                                "enable_proper_noun", "payment_entitlement"):
                        self.assertEqual(getattr(plan, key), getattr(before, key), key)
                    self.assertEqual(plan.translation_stats["translation_preflight"],
                                     before.translation_stats["translation_preflight"])
                    self.assertEqual(plan.translation_stats["attempt_id"], before.translation_stats["attempt_id"])
                    self.assertEqual(plan.translation_stats["translation_input"], before.translation_stats["translation_input"])
                    normalized = Path(kwargs["input_path"])
                    self.assertEqual(normalized.suffix, ".epub")
                    self.assertTrue(zipfile.is_zipfile(normalized))
                    manifest = build_manifest(str(normalized), job.id)
                    self.assertFalse(manifest.get("error"), manifest)
                    body_ids = {row["chapter_id"] for row in manifest["chapters"] if row.get("chapter_kind") == "body"}
                    self.assertTrue(set(before.translation_stats["translation_preflight"]["chapter_strategy_overrides"]) <= body_ids)
                    self.assertEqual(Path(plan.input_path).read_bytes(), self.payload(extension))
                    self.assertFalse(kwargs["cancel_check"]())
                    kwargs["stage_callback"]("offline_executor", "captured full paid plan")
                    Path(kwargs["output_path"]).write_bytes(self.output_bytes)
                    seen.append(normalized)
                    return self.ConversionResult(translation_stats={"translated_chunks": 1, "failed_chunks": 0})

                finished, executor, old_converter = self.execute(job, capture)
                executor.assert_called_once()
                old_converter.assert_not_called()
                self.assertEqual(finished.status, self.JobStatus.success, finished.message)
                self.assertEqual(Path(finished.output_path).read_bytes(), self.output_bytes)
                self.assertEqual(finished.payment_entitlement, before.payment_entitlement)
                self.assertEqual(finished.translation_stats["translation_preflight"], before.translation_stats["translation_preflight"])
                self.assertEqual(finished.translation_stats["artifact_audit"]["status"], "passed")
                download = self.client.get("/jobs/" + job.id + "/download", headers={"X-Job-Token": job.access_token})
                self.assertEqual(download.status_code, 200, download.text if download.status_code != 200 else "")
                self.assertEqual(download.content, self.output_bytes)
                if extension != ".epub":
                    self.assertFalse(seen[0].exists(), "worker normalization directory leaked")

    def test_verified_retry_preserves_normalized_identity_and_confirmed_chapters(self):
        job = self.confirmed_paid_job(".docx")
        identity = copy.deepcopy(job.translation_stats["translation_input"])
        preflight = copy.deepcopy(job.translation_stats["translation_preflight"])
        self.store.update_status(job.id, self.JobStatus.failed, "offline interrupted attempt")
        restarted, reason = self.store.restart_translation_attempt(job.id, attempt_id="r4-safe-retry",
            started_at=datetime.now(timezone.utc), max_free_retries=-1, action_label="offline retry")
        self.assertEqual(reason, "ok")
        self.assertEqual(restarted.payment_entitlement, job.payment_entitlement)
        self.assertEqual(restarted.translation_stats["translation_input"], identity)
        self.assertEqual(restarted.translation_stats["translation_preflight"], preflight)
        finished, executor, converter = self.execute(restarted, expected_attempt_id="r4-safe-retry")
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)
        self.assertEqual(executor.call_args.kwargs["job"].translation_stats["translation_input"], identity)
        self.assertEqual(finished.translation_stats["translation_preflight"], preflight)
        converter.assert_not_called()

    def test_worker_kill_switch_does_not_fallback_or_publish_paid_book(self):
        job = self.confirmed_paid_job(".docx")
        with patch.dict(os.environ, {"EPUB_FAST_TRANSLATION": "0"}):
            finished, executor, old_converter = self.execute(job)
        self.assertEqual(finished.status, self.JobStatus.failed)
        self.assertFalse(finished.output_path)
        self.assertEqual(finished.payment_entitlement, job.payment_entitlement)
        executor.assert_not_called()
        old_converter.assert_not_called()
        self.assertEqual(list(self.outputs.iterdir()), [])

    def test_worker_normalization_failure_is_fail_closed(self):
        job = self.confirmed_paid_job(".docx")
        Path(job.input_path).write_bytes(b"corrupt Word after verified payment")
        finished, executor, old_converter = self.execute(job)
        self.assertEqual(finished.status, self.JobStatus.failed)
        self.assertFalse(finished.output_path)
        executor.assert_not_called()
        old_converter.assert_not_called()
        self.assertEqual(list(self.outputs.iterdir()), [])

    def test_paid_worker_rejects_different_valid_source_before_model(self):
        job = self.confirmed_paid_job(".md")
        Path(job.input_path).write_text("# Changed\n\nAnother valid manuscript after payment.\n")
        finished, executor, converter = self.execute(job)
        self.assertEqual(finished.status, self.JobStatus.failed)
        self.assertFalse(finished.output_path)
        executor.assert_not_called()
        converter.assert_not_called()
        self.assertEqual(list(self.outputs.iterdir()), [])

    def test_cancellation_during_actual_docx_normalization_prevents_executor(self):
        from app.engine.adapters import docx_adapter
        job = self.confirmed_paid_job(".docx")
        real_adapter = docx_adapter.docx_to_html
        paths = []

        def cancelled_adapter(path):
            result = real_adapter(path)
            paths.append(Path(path))
            self.store.update_status(job.id, self.JobStatus.cancelled, "cancelled while adapting Word")
            return result

        with patch.object(docx_adapter, "docx_to_html", side_effect=cancelled_adapter):
            finished, executor, old_converter = self.execute(job)
        self.assertEqual(finished.status, self.JobStatus.cancelled)
        self.assertFalse(finished.output_path)
        executor.assert_not_called()
        old_converter.assert_not_called()
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())
        self.assertEqual(list(self.outputs.iterdir()), [])

    def test_executor_failure_cannot_fallback_or_deliver_partial_output(self):
        job = self.confirmed_paid_job(".md")

        def fail(**kwargs):
            Path(kwargs["output_path"]).write_bytes(self.output_bytes)
            raise RuntimeError("offline executor failed after partial work")

        finished, executor, old_converter = self.execute(job, fail)
        self.assertEqual(finished.status, self.JobStatus.failed)
        self.assertFalse(finished.output_path)
        executor.assert_called_once()
        old_converter.assert_not_called()
        self.assertEqual(list(self.outputs.iterdir()), [])
        self.assertFalse(self.executor_paths[0].exists())

    def test_cancellation_after_executor_started_never_publishes_artifact(self):
        job = self.confirmed_paid_job(".md")

        def cancel(**kwargs):
            self.store.update_status(job.id, self.JobStatus.cancelled, "offline cancellation")
            self.assertTrue(kwargs["cancel_check"]())
            Path(kwargs["output_path"]).write_bytes(self.output_bytes)
            return self.ConversionResult(translation_stats={"translated_chunks": 1})

        finished, executor, old_converter = self.execute(job, cancel)
        self.assertEqual(finished.status, self.JobStatus.cancelled)
        self.assertFalse(finished.output_path)
        executor.assert_called_once()
        old_converter.assert_not_called()
        self.assertEqual(list(self.outputs.iterdir()), [])
        self.assertFalse(self.executor_paths[0].exists())

    def test_superseded_attempt_cannot_publish_or_overwrite_new_state(self):
        job = self.confirmed_paid_job(".docx")
        newer = copy.deepcopy(job.translation_stats)
        newer.update(attempt_id="newer-r4-attempt", marker="new owner's state")
        saved_newer = []

        def supersede(**kwargs):
            # The retry is an independent command actor, not a business write
            # inheriting the old executor's ContextVar identity.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as commands:
                commands.submit(self.store.update_status, job.id, self.JobStatus.pending,
                                "newer attempt waiting", translation_stats=newer).result()
            saved_newer.append(copy.deepcopy(self.store.get(job.id).translation_stats))
            self.assertTrue(kwargs["cancel_check"]())
            Path(kwargs["output_path"]).write_bytes(self.output_bytes)
            return self.ConversionResult(translation_stats={"translated_chunks": 1})

        finished, executor, old_converter = self.execute(job, supersede)
        self.assertEqual(finished.status, self.JobStatus.pending)
        self.assertEqual(finished.translation_stats, saved_newer[0])
        self.assertEqual(finished.message, "newer attempt waiting")
        self.assertFalse(finished.output_path)
        executor.assert_called_once()
        old_converter.assert_not_called()
        self.assertEqual(list(self.outputs.iterdir()), [])
        self.assertFalse(self.executor_paths[0].exists())

    def test_stale_queued_attempt_is_rejected_before_normalization(self):
        job = self.confirmed_paid_job(".docx")
        original = copy.deepcopy(job.translation_stats)
        Path(job.input_path).unlink()  # Any source access would now fail.
        finished, executor, old_converter = self.execute(job, expected_attempt_id="stale-r4-attempt")
        self.assertEqual(finished.status, self.JobStatus.pending)
        self.assertEqual(finished.translation_stats, original)
        executor.assert_not_called()
        old_converter.assert_not_called()

    def test_ordinary_conversion_stays_on_existing_converter_when_translation_disabled(self):
        from app.domain import fast_translation_runner
        with patch.dict(os.environ, {"EPUB_FAST_TRANSLATION": "0"}):
            job = self.job_from(self.create(".md", enable_translation="false", profile_confirmation="false"))
            self.assertEqual(job.status, self.JobStatus.pending_payment)
            self.store.update_status(job.id, self.JobStatus.pending, "ordinary conversion ready")

            def convert(source, destination, mode, **kwargs):
                self.assertEqual(Path(source), Path(job.input_path))
                self.assertFalse(kwargs["enable_translation"])
                Path(destination).write_bytes(epub_bytes())
                return self.ConversionResult()

            with patch.object(self.runner.converter, "convert_file_to_horizontal", side_effect=convert) as converter, \
                 patch.object(fast_translation_runner, "run_fast_translation_job") as executor:
                self.runner.run_job(job.id)
                converter.assert_called_once()
                executor.assert_not_called()
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.success)
        self.preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
