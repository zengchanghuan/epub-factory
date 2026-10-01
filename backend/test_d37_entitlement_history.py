"""R1: offline purchase-entitlement regression using opt-in historical books.

Run from any directory with EPUB_HISTORY_UPLOAD_DIR and EPUB_HISTORY_OUTPUT_DIR
pointing at the existing historical directories. Without both variables the
suite explicitly skips; customer books are never copied into the repository.

This exercises real EPUB upload/resource validation, price parsing, persistent
job state, retry authorization and historical artifact downloads. Book profiling
and the payment page gateway are simulated; dispatch is captured, never run.
It does NOT evaluate new translation quality or verify a live payment provider.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

BOOKS = (
    {
        "key": "double-helix",
        "input": "9ea9e22f5a62-The Annotated and Illustrated Double Helix.epub",
        "input_sha256": "bf0e8509e16c6a43f148d1671a989f0ff6558821f4f3effee55314e920c14089",
        "output": "9ea9e22f5a62-The Annotated and Illustrated Double Helix_简体_3.epub",
        "output_sha256": "aa2cbfc8077e8e76ffc3e349063786315221d8080206e63bb11235141e03533e",
    },
    {
        "key": "die-with-zero",
        "input": "27791e419e56-別把你的錢留到死：懂得花錢，是最好的投資——理想人生的9大財務思維.epub",
        "input_sha256": "d341123889d2bd2b84d9037528ca1c28b20440281f80aa45ff84ab6318f1e900",
        "output": "別把你的錢留到死：懂得花錢，是最好的投資——理想人生的9大財務思維-横排简体.epub",
        "output_sha256": "6ce98acc076930752e3ccbcf26bda70b9e43d22b3b41a2a168b2ff11bfb93b59",
    },
    {
        "key": "responsibility-and-judgement",
        "input": "13f844173d75-責任與判斷 = Responsibility and Judgemen (漢娜 · 鄂蘭 (Hannah Arendt) 著；蔡佩君 譯) (z-library.sk, 1lib.sk, z-lib.sk).epub",
        "input_sha256": "d3e1ec97ab2a61fd95f042eec88d9af119b16413f5eb5838f604946bd63a3917",
        "output": "责任与判断 = Responsibility and Judgemen (汉娜 · 鄂兰 (Hannah Arendt) 着；蔡佩君 译) (z-library.sk, 1lib.sk, z-lib.sk)_简体_2.epub",
        "output_sha256": "5c498c2dd7e56476c9e0ed373f8b834227f8b9744cd0aaaa3bc1e03e704ed264",
    },
)


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(source.read()).hexdigest()


@unittest.skipUnless(
    os.environ.get("EPUB_HISTORY_UPLOAD_DIR") and os.environ.get("EPUB_HISTORY_OUTPUT_DIR"),
    "Historical EPUB regression requires explicit EPUB_HISTORY_UPLOAD_DIR and EPUB_HISTORY_OUTPUT_DIR; no synthetic fallback.",
)
class HistoricalEntitlementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history_uploads = Path(os.environ["EPUB_HISTORY_UPLOAD_DIR"]).expanduser().resolve()
        cls.history_outputs = Path(os.environ["EPUB_HISTORY_OUTPUT_DIR"]).expanduser().resolve()
        for book in BOOKS:
            for kind, directory in (("input", cls.history_uploads), ("output", cls.history_outputs)):
                path = directory / book[kind]
                if not path.is_file():
                    raise AssertionError(f"Explicit historical fixture missing: {path}")
                if sha256(path) != book[kind + "_sha256"]:
                    raise AssertionError(f"Historical fixture changed: {book['key']} {kind}")

        cls.runtime = tempfile.TemporaryDirectory(prefix="epub-r1-history-")
        cls.addClassCleanup(cls.runtime.cleanup)
        cls.root = Path(cls.runtime.name)
        cls.guards = ExitStack()
        cls.addClassCleanup(cls.guards.close)
        cls.guards.enter_context(patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(cls.root / "bootstrap.sqlite3"),
            "EPUB_PERSISTENT_STORE": "1",
            "REPAIR_UPLOAD_DIR": str(cls.root / "repairs"),
            "EPUB_TRANSLATION_CHECKPOINT_DB": str(cls.root / "checkpoints.sqlite3"),
            "OPENAI_API_KEY": "offline-history-never-use",
            "OPENAI_BASE_URL": "http://offline.invalid/v1",
            "ALIPAY_APP_ID": "",
            "CELERY_BROKER_URL": "",
            "REDIS_URL": "",
            "SKIP_PAYMENT_CHECK": "0",
            "SENTRY_DSN": "",
            "NOTIFY_EMAIL_ENABLED": "0",
            "OWNER_PAYMENT_EMAIL_ENABLED": "0",
            "SMTP_HOST": "",
            "ADMIN_SECRET": "offline-history-admin",
            "DOWNLOAD_SIGN_SECRET": "offline-history-download-signature",
        }))
        cls.guards.enter_context(patch("dotenv.load_dotenv", return_value=False))
        cls.network_guards = [
            cls.guards.enter_context(patch(target, side_effect=AssertionError("Historical R1 regression forbids network access")))
            for target in ("socket.socket.connect", "socket.create_connection", "requests.sessions.Session.request")
        ]
        from app import main
        from app import storage
        from app.engine.translation_cache import TranslationCache
        cls.main = main
        # The real pricing parser uses an empty isolated cache, never the user's cache.
        cls.guards.enter_context(patch(
            "app.engine.translation_cache.TranslationCache",
            side_effect=lambda *args, **kwargs: TranslationCache(str(cls.root / "pricing-cache.sqlite3")),
        ))
        if hasattr(storage.job_store, "_engine"):
            cls.addClassCleanup(storage.job_store._engine.dispose)

    def setUp(self):
        from sqlalchemy import create_engine
        from app.storage_db import Base, PersistentJobStore
        from fastapi.testclient import TestClient

        self.tmp = tempfile.TemporaryDirectory(prefix="case-", dir=self.root)
        self.addCleanup(self.tmp.cleanup)
        self.case_root = Path(self.tmp.name)
        self.uploads = self.case_root / "uploads"
        self.outputs = self.case_root / "outputs"
        self.uploads.mkdir()
        self.outputs.mkdir()
        engine = create_engine("sqlite:///" + str(self.case_root / "jobs.sqlite3"))
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.store = PersistentJobStore(engine=engine)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in (("job_store", self.store), ("UPLOAD_DIR", self.uploads),
                            ("OUTPUT_DIR", self.outputs), ("DOWNLOAD_SIGN_SECRET", "offline-history-download-signature")):
            self.patches.enter_context(patch.object(self.main, name, value))
        self.enqueue = self.patches.enter_context(patch.object(self.main, "_enqueue_conversion"))
        self.worker = self.patches.enter_context(patch.object(
            self.main, "process_job", side_effect=AssertionError("No real worker execution in R1 history regression")))
        self.profile = self.patches.enter_context(patch.object(self.main, "build_translation_preflight", return_value={
            "version": 1, "test_fixture": "history-offline-v1", "resolved_strategy": "neutral_faithful",
            "glossary": {}, "characters": [], "chapters": [], "source_warnings": [],
        }))
        self.payment = self.patches.enter_context(patch.object(
            self.main, "create_alipay_page_pay", return_value="https://offline.invalid/payment"))
        self.client = TestClient(self.main.app)
        self.addCleanup(self.client.close)

    def tearDown(self):
        self.worker.assert_not_called()
        for guard in self.network_guards:
            guard.assert_not_called()
        for book in BOOKS:
            self.assertEqual(sha256(self.history_uploads / book["input"]), book["input_sha256"])
            self.assertEqual(sha256(self.history_outputs / book["output"]), book["output_sha256"])

    def create_waiting(self, book):
        from app.models import JobStatus
        self.enqueue.reset_mock()
        with (self.history_uploads / book["input"]).open("rb") as source:
            response = self.client.post("/api/v2/jobs", files={
                "file": (book["input"], source, "application/epub+zip"),
            }, data={
                "enable_translation": "true", "profile_confirmation": "true",
                "output_mode": "simplified", "translation_model": "deepseek-flash",
                "translation_quality": "standard", "cache_policy": "reuse",
            })
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        job = self.store.get(payload["job_id"])
        self.assertEqual(job.status, JobStatus.awaiting_confirmation)
        self.assertEqual(sha256(Path(job.input_path)), book["input_sha256"])
        self.assertTrue(Path(job.input_path).is_relative_to(self.uploads))
        self.assertEqual(job.payment_entitlement["state"], "quoted")
        self.enqueue.assert_not_called()
        return job, {"X-Job-Token": payload["access_token"]}

    def create_paid_failed(self, book):
        from app.models import ErrorCode, JobStatus
        from app.domain.payment_entitlement import grant_verified_entitlement
        job, headers = self.create_waiting(book)
        confirmed = self.client.post(f"/api/v2/jobs/{job.id}/confirm-profile", headers=headers, json={})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.pending_payment)
        # Seed the server-verified gateway boundary; do not claim a live payment.
        entitlement = grant_verified_entitlement(self.store, job, job.expected_amount, "verified_query")
        self.assertEqual(entitlement["state"], "paid")
        self.assertTrue(self.store.try_mark_paid(job.id))
        self.store.update_status(job.id, JobStatus.failed, "offline prior QA failure", error_code=ErrorCode.PARTIAL_TRANSLATION.value)
        self.enqueue.reset_mock()
        return self.store.get(job.id), headers

    def test_unpaid_cancel_cannot_restart(self):
        from app.models import JobStatus
        for book in BOOKS:
            with self.subTest(book=book["key"]):
                job, headers = self.create_waiting(book)
                cancelled = self.client.post(f"/api/v2/jobs/{job.id}/cancel", headers=headers)
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                before = self.store.get(job.id)
                response = self.client.post(f"/api/v2/jobs/{job.id}/restart-translation", headers=headers)
                self.assertEqual(response.status_code, 402, response.text)
                after = self.store.get(job.id)
                self.assertEqual(after.status, JobStatus.cancelled)
                self.assertEqual(after.translation_stats, before.translation_stats)
                self.enqueue.assert_not_called()

    def test_verified_original_plan_can_restart_and_retry(self):
        from app.models import JobStatus
        for book in BOOKS:
            for action in ("restart-translation", "retry-translation"):
                with self.subTest(book=book["key"], action=action):
                    job, headers = self.create_paid_failed(book)
                    response = self.client.post(f"/api/v2/jobs/{job.id}/{action}", headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.enqueue.assert_called_once()
                    after = self.store.get(job.id)
                    self.assertEqual(after.status, JobStatus.pending)
                    self.assertEqual(after.payment_entitlement, job.payment_entitlement)
                    self.assertEqual(after.translation_model, "deepseek-flash")
                    self.assertEqual(after.translation_quality, "standard")
                    self.assertEqual(sha256(Path(after.input_path)), book["input_sha256"])

    def test_pro_and_literary_upgrades_are_denied(self):
        for book in BOOKS:
            job, headers = self.create_paid_failed(book)
            for params in ({"translation_model": "deepseek-v4-pro"}, {"translation_quality": "literary"}):
                with self.subTest(book=book["key"], requested=params):
                    response = self.client.post(f"/api/v2/jobs/{job.id}/restart-translation", headers=headers, params=params)
                    self.assertEqual(response.status_code, 409, response.text)
                    after = self.store.get(job.id)
                    self.assertEqual(after.status, job.status)
                    self.assertEqual(after.translation_stats, job.translation_stats)
                    self.assertEqual(after.payment_entitlement, job.payment_entitlement)
                    self.assertEqual(after.translation_model, job.translation_model)
                    self.assertEqual(after.translation_quality, job.translation_quality)
                    self.enqueue.assert_not_called()

    def test_historical_downloads_survive_store_reload_unchanged(self):
        from app.models import Job, JobStatus, OutputMode
        from app.storage_db import PersistentJobStore
        for book in BOOKS:
            with self.subTest(book=book["key"]):
                output = self.outputs / book["output"]
                shutil.copyfile(self.history_outputs / book["output"], output)
                job = Job(id=book["key"], trace_id="offline-history", source_filename=book["input"],
                          input_path=str(self.history_uploads / book["input"]), output_path=str(output),
                          output_mode=OutputMode.simplified, status=JobStatus.success,
                          access_token="history-access-" + book["key"], creator_session="history-original-session",
                          token_expires_at=datetime.now(timezone.utc) + timedelta(days=1))
                self.store.add(job)  # Deliberately no entitlement: existing downloads must remain accessible.
                reloaded = PersistentJobStore(engine=self.store._engine)
                with patch.object(self.main, "job_store", reloaded):
                    headers = {"X-Job-Token": job.access_token, "X-Client-Session": job.creator_session}
                    listing = self.client.get("/api/v2/jobs", headers=headers)
                    self.assertEqual(listing.status_code, 200, listing.text)
                    self.assertTrue(any(item["job_id"] == job.id for item in listing.json()["items"]))
                    detail = self.client.get(f"/api/v2/jobs/{job.id}", headers=headers)
                    self.assertEqual(detail.status_code, 200, detail.text)
                    self.assertEqual(detail.json()["status"], "completed")
                    self.assertEqual(detail.headers["cache-control"], "no-store")
                    downloaded = self.client.get(detail.json()["download_url"])
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), book["output_sha256"])
                self.assertEqual(sha256(output), book["output_sha256"])
                self.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
