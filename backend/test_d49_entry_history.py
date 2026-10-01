"""R11 opt-in history: three entry intents share one HTTP/payment lifecycle.

The D40 fixture supplies real preprocessing, exact chapter-cache identity replay,
packaging, EPUBCheck and independent content/navigation checks for three SHA-
pinned books. Conversion entries additionally execute the real job runner.

Translation completion is a CONTROLLED terminal-state fixture using that real
identity-replayed artifact. It does not execute a translator, change QA policy,
mock QA to pass, or claim English-to-Chinese/semantic translation quality. This
suite covers backend lifecycle/authorization; frontend VM tests prove that the
three landing pages actually choose these request parameters.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import test_d37_entitlement_history as entitlement
import test_d40_reduce_history as replay

navigation = replay.tables.navigation
# A supported user-selected source profile, not an assertion that every entry
# overwrites the homepage's default/restored profile. Frontend F29 owns that map.
ENTRY_FORMS = {
    "translate": {"enable_translation": "true", "profile_confirmation": "true",
                  "translation_model": "deepseek-flash", "translation_quality": "standard",
                  "cache_policy": "reuse", "output_mode": "simplified", "traditional_variant": "auto"},
    "horizontal": {"enable_translation": "false", "output_mode": "simplified", "traditional_variant": "auto"},
    "simplified": {"enable_translation": "false", "output_mode": "simplified", "traditional_variant": "auto"},
}


class EntryHistoryTests(replay.ReduceHistoryTests):
    @classmethod
    def setUpClass(cls):
        dns_patch = patch("socket.getaddrinfo", side_effect=AssertionError("R11 history forbids external DNS"))
        dns_guard = dns_patch.start()
        cls.addClassCleanup(dns_patch.stop)
        super().setUpClass()
        from app import main
        from app.engine import translation_cache
        cls.main = main
        cls.history_uploads, cls.history_outputs = cls.uploads, cls.deliveries
        cls.network_guards = cls.guards
        cls.guards.append(dns_guard)
        real_cache = translation_cache.TranslationCache
        cls.stack.enter_context(patch.object(
            translation_cache, "TranslationCache",
            side_effect=lambda *_a, **_k: real_cache(str(cls.root / "r11-quote-cache.sqlite3")),
        ))
        cls.stack.enter_context(patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "", "DASHSCOPE_API_KEY": "", "GEMINI_API_KEY": "",
            "SKIP_PAYMENT_CHECK": "0", "ALIPAY_APP_ID": "", "ALIPAY_SELLER_ID": "",
            "JOB_DISPATCH_ENABLED": "0", "ADMIN_SECRET": "offline-r11-admin",
        }))

    def setUp(self):
        self.real_enqueue = self.main._enqueue_conversion
        entitlement.HistoricalEntitlementTests.setUp(self)

    def tearDown(self):
        entitlement.HistoricalEntitlementTests.tearDown(self)

    def entry_runtime(self):
        """Use actual durable dispatch; replace only the external transport."""
        from app import job_runner
        self.patches.enter_context(patch.object(self.main, "_enqueue_conversion", self.real_enqueue))
        self.patches.enter_context(patch.object(self.main, "_use_celery", return_value=True))
        self.patches.enter_context(patch.object(self.main, "verify_alipay_notification", return_value=True))
        self.patches.enter_context(patch("app.infra.alipay.create_alipay_precreate", return_value=None))
        self.patches.enter_context(patch("app.domain.payment_email_service.queue_paid_order_email"))
        self.patches.enter_context(patch.object(job_runner, "job_store", self.store))
        self.patches.enter_context(patch.object(job_runner, "OUTPUT_DIR", self.outputs))
        self.patches.enter_context(patch.object(job_runner, "notify_job_completed"))
        self.patches.enter_context(patch.object(job_runner, "report_error"))
        self.patches.enter_context(patch("app.infra.execution_lease.tempfile.gettempdir", return_value=str(self.case_root)))
        self.messages = []
        self.transport = self.patches.enter_context(patch(
            "app.infra.job_dispatch_publisher.publish_conversion",
            side_effect=lambda job_id, attempt_id: self.messages.append((job_id, attempt_id)),
        ))
        return job_runner

    def create_entry(self, book, entry):
        from app.models import JobStatus
        before_ids = {job.id for job in self.store.list_jobs()}
        fields = {**ENTRY_FORMS[entry], "enable_precision_polish": "false",
                  "lexicon_domains_json": "[]", "enable_proper_noun": "false"}
        with (self.history_uploads / book["input"]).open("rb") as source:
            response = self.client.post("/api/v2/jobs", files={
                "file": (book["input"], source, "application/epub+zip"),
            }, data=fields, headers={"X-Client-Session": "r11-history-session"})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        headers = {"X-Job-Token": data["access_token"], "X-Client-Session": "r11-history-session"}
        job = self.store.get(data["job_id"])
        self.assertEqual(job.enable_translation, entry == "translate")
        self.assertFalse(job.enable_precision_polish)
        self.assertEqual(job.traditional_variant, "auto")
        self.assertEqual(navigation.sha256(Path(job.input_path)), book["input_sha256"])
        self.assertEqual({item.id for item in self.store.list_jobs()}, before_ids | {job.id})
        if entry == "translate":
            self.assertEqual(job.status, JobStatus.awaiting_confirmation)
            self.assertIsNone(self.client.get(f"/api/v2/jobs/{job.id}", headers=headers).json()["download_url"])
            self.assertEqual(self.store.list_dispatches(job.id), [])
            confirm = self.client.post(f"/api/v2/jobs/{job.id}/confirm-profile", headers=headers, json={})
            self.assertEqual(confirm.status_code, 200, confirm.text)
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.pending_payment)
        self.assertEqual(self.store.list_dispatches(job.id), [])
        self.assertFalse([item for item in self.messages if item[0] == job.id])
        self.assertFalse(job.output_path)
        self.assertEqual(self.client.get(f"/api/v2/jobs/{job.id}/download", headers=headers).status_code, 400)
        return job, headers

    def reload(self):
        from app import job_runner
        from app.storage_db import PersistentJobStore
        self.store = PersistentJobStore(self.store._engine)
        self.main.job_store = job_runner.job_store = self.store

    def receipt(self, job):
        return {"out_trade_no": job.id, "total_amount": job.expected_amount,
                "trade_status": "TRADE_SUCCESS", "sign": "offline-verified-boundary"}

    def refresh_without_new_order(self, job, headers, *, paid):
        before = self.store.get(job.id)
        before_ids = {item.id for item in self.store.list_jobs()}
        payment_calls = self.payment.call_count
        publish_calls = self.transport.call_count
        self.reload()
        detail = self.client.get(f"/api/v2/jobs/{job.id}", headers=headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.headers["cache-control"], "no-store")
        trade = {**self.receipt(job), "trade_status": "TRADE_SUCCESS" if paid else "WAIT_BUYER_PAY"}
        with patch("app.infra.alipay.query_verified_trade", return_value=trade):
            recover = self.client.post(f"/api/v2/jobs/{job.id}/recover", headers=headers)
        self.assertEqual(recover.status_code, 200, recover.text)
        current = self.store.get(job.id)
        self.assertEqual(current.id, before.id)
        self.assertEqual(current.status, before.status)
        self.assertEqual(current.expected_amount, before.expected_amount)
        self.assertEqual(current.input_path, before.input_path)
        self.assertEqual(current.payment_entitlement, before.payment_entitlement)
        self.assertEqual({item.id for item in self.store.list_jobs()}, before_ids)
        self.assertEqual(self.payment.call_count, payment_calls, "Refresh created another payment order")
        self.assertEqual(self.transport.call_count, publish_calls, "Refresh duplicated a sent dispatch")
        return detail.json()

    def verify_payment_once(self, job):
        from app.models import JobStatus
        calls = self.transport.call_count
        for _ in range(2):
            callback = self.client.post("/api/v2/webhooks/alipay", data=self.receipt(job))
            self.assertEqual(callback.text, "success")
        self.assertEqual(self.transport.call_count, calls + 1)
        current = self.store.get(job.id)
        self.assertEqual(current.status, JobStatus.pending)
        self.assertEqual(current.expected_amount, job.expected_amount)
        records = self.store.list_dispatches(job.id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "sent")
        self.assertEqual(records[0]["attempts"], 1)
        captured = [item for item in self.messages if item[0] == job.id]
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][1], str((current.translation_stats or {}).get("attempt_id") or ""))
        return captured[0]

    def assert_delivery(self, book, job, headers):
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub
        from app.models import JobStatus
        delivered = self.store.get(job.id)
        self.assertEqual(delivered.status, JobStatus.success, delivered.message)
        self.assertTrue(validate_epub(delivered.output_path, EPUBCHECK_JAR).passed)
        actual = navigation.BookSnapshot(delivered.output_path, self.opencc)
        expected = self.runs[book["key"]][1]
        self.assertEqual(actual.images, expected.images)
        self.assertEqual(set(actual.docs), set(expected.docs))
        for name, document in expected.docs.items():
            self.assertTrue(actual.docs[name]["text"] == document["text"], name)
            self.assertLessEqual(document["ids"], actual.docs[name]["ids"])
        self.assertEqual([(row["label"], row["target"], row["depth"]) for row in actual.toc],
                         [(row["label"], row["target"], row["depth"]) for row in expected.toc])
        detail = self.refresh_without_new_order(delivered, headers, paid=True)
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(self.client.get(f"/api/v2/jobs/{job.id}/download",
                                        headers={"X-Job-Token": "wrong-token"}).status_code, 403)
        download = self.client.get(detail["download_url"], headers=headers)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(hashlib.sha256(download.content).hexdigest(), navigation.sha256(Path(delivered.output_path)))
        self.assertEqual(navigation.sha256(Path(delivered.input_path)), book["input_sha256"])

    def test_entry_conversion_intents_share_payment_recovery_and_real_execution(self):
        runner = self.entry_runtime()
        records = []
        for entry in ("horizontal", "simplified"):
            for book in navigation.BOOKS:
                with self.subTest(entry=entry, book=book["key"]):
                    job, headers = self.create_entry(book, entry)
                    self.refresh_without_new_order(job, headers, paid=False)
                    message = self.verify_payment_once(job)
                    self.refresh_without_new_order(job, headers, paid=True)
                    with patch.object(runner, "_run_job_locked", wraps=runner._run_job_locked) as executing, redirect_stdout(io.StringIO()):
                        runner.run_job(*message)
                        runner.run_job(*message)
                    self.assertEqual(executing.call_count, 1)
                    self.assert_delivery(book, job, headers)
                    records.append({"entry": entry, "book": book["key"], "dispatches": 1, "executions": 1})
        print("R11 real conversion lifecycle evidence: " + json.dumps(records, sort_keys=True))

    def test_entry_translation_lifecycle_with_controlled_historical_identity_delivery(self):
        self.entry_runtime()
        from app.domain.job_write_fence import job_write_scope
        from app.infra.execution_lease import execution_lease
        from app.models import JobStatus
        records = []
        for book in navigation.BOOKS:
            with self.subTest(book=book["key"]):
                job, headers = self.create_entry(book, "translate")
                self.refresh_without_new_order(job, headers, paid=False)
                _key, attempt = self.verify_payment_once(job)
                self.refresh_without_new_order(job, headers, paid=True)
                # Controlled completion boundary, analogous to D37 historical
                # delivery seeding. Never bypass the production semantic QA
                # gate and then describe an identity replay as a new translation.
                artifact = self.outputs / (book["key"] + "-identity-replay.epub")
                replayed = self.root / (book["key"] + "-replayed.epub")
                self.assertTrue(self.replay_validation[book["key"]].passed)
                shutil.copyfile(replayed, artifact)
                with execution_lease(job.id, attempt) as lease:
                    self.assertIsNotNone(lease)
                    self.assertTrue(self.store.begin_execution(job.id, attempt, lease.owner))
                    stats = {**self.store.get(job.id).translation_stats,
                             "r11_evidence": "controlled historical identity delivery; no model or semantic QA claim"}
                    with job_write_scope(job.id, attempt, lease.owner):
                        self.store.update_status(job.id, JobStatus.success, "历史身份回放：仅验证入口与交付契约",
                                                 output_path=str(artifact), translation_stats=stats,
                                                 expected_attempt_id=attempt)
                    self.assertTrue(self.store.finish_execution(job.id, attempt, lease.owner))
                self.assert_delivery(book, job, headers)
                self.assertEqual(navigation.sha256(artifact), navigation.sha256(replayed))
                records.append({"book": book["key"], "entry": "translate", "dispatches": 1,
                                "body_chapters_replayed": len(self.replay_records[book["key"]]["body_paths"]),
                                "artifact_sha256": navigation.sha256(artifact), "model_calls": 0})
        print("R11 controlled identity-delivery evidence (NOT translation quality): " + json.dumps(records, sort_keys=True))

    def test_entry_paid_cancellation_and_refresh_never_deliver_or_restart(self):
        runner = self.entry_runtime()
        from app.models import JobStatus
        for entry in ENTRY_FORMS:
            for book in navigation.BOOKS:
                with self.subTest(entry=entry, book=book["key"]):
                    job, headers = self.create_entry(book, entry)
                    message = self.verify_payment_once(job)
                    cancelled = self.client.post(f"/api/v2/jobs/{job.id}/cancel", headers=headers)
                    self.assertEqual(cancelled.status_code, 200, cancelled.text)
                    self.assertEqual(self.store.get(job.id).status, JobStatus.cancelled)
                    with patch.object(runner, "_run_job_locked", wraps=runner._run_job_locked) as executing:
                        runner.run_job(*message)
                    executing.assert_not_called()
                    payment_calls, publish_calls = self.payment.call_count, self.transport.call_count
                    before_ids = {item.id for item in self.store.list_jobs()}
                    self.reload()
                    for _ in range(2):
                        self.assertEqual(self.client.post("/api/v2/webhooks/alipay", data=self.receipt(job)).text, "success")
                    with patch("app.infra.alipay.query_verified_trade", return_value=self.receipt(job)):
                        response = self.client.post(f"/api/v2/jobs/{job.id}/recover", headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    detail = self.client.get(f"/api/v2/jobs/{job.id}", headers=headers)
                    self.assertEqual(detail.json()["status"], "cancelled")
                    self.assertIsNone(detail.json()["download_url"])
                    self.assertEqual(self.client.get(f"/api/v2/jobs/{job.id}/download", headers=headers).status_code, 400)
                    self.assertEqual(self.payment.call_count, payment_calls)
                    self.assertEqual(self.transport.call_count, publish_calls)
                    self.assertEqual({item.id for item in self.store.list_jobs()}, before_ids)
                    self.assertFalse(self.store.get(job.id).output_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
