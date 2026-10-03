"""PDF checkout time/content gates with real temporary files and SQLite.

Only provider/dispatch/mail boundaries are mocked. Synthetic file bytes test
fingerprints, not PDF layout or EPUBCheck; those have separate real-book gates.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv", return_value=False):
    import test_d59_pdf_store as fixtures
    from app.tasks import reconcile

from app.domain.checkout_resume import (CheckoutUnavailable, checkout_created_at, checkout_snapshot,
                                       original_checkout, require_open_checkout)
from app.domain.pdf_product import new_pdf_plan, pdf_plan_identity
from app.models import JobStatus, OutputMode
from app.storage_db import PersistentJobStore


class PdfCheckoutTests(unittest.TestCase):
    def setUp(self):
        fixtures.PdfStoreTests.setUp(self)
        self.root = Path(self.engine.url.database).parent.resolve()
        self.source = self.root / "synthetic.pdf"
        self.source.write_bytes(b"synthetic PDF fingerprint input; not a parser test")
        self.assets = self.root / "outputs"
        self.assets.mkdir()
        artifact_dir = self.assets / (".pdf-prepared-" + "b" * 32)
        artifact_dir.mkdir()
        self.artifact = artifact_dir / "book.epub"
        self.artifact.write_bytes(b"synthetic committed artifact fingerprint")
        self.now = datetime.now(timezone.utc)
        self.stack.enter_context(patch.dict(os.environ, {"RECONCILE_TIMEOUT_HOURS": "2"}))
        self.stack.enter_context(patch.object(reconcile, "job_store", self.sql))
        self.stack.enter_context(patch.object(reconcile, "_TIMEOUT_HOURS", 2))
        self.query = self.stack.enter_context(patch.object(reconcile, "query_verified_trade"))
        self.close = self.stack.enter_context(patch.object(reconcile, "close_verified_trade", return_value=None))
        self.dispatch = self.stack.enter_context(patch.object(reconcile, "_publish_conversion"))
        self.stack.enter_context(patch.object(reconcile, "_queue_paid_email"))
        self.stack.enter_context(patch.object(reconcile, "record_event"))

    def job(self, key="book", *, checkout_at=None, **overrides):
        data = self.source.read_bytes()
        plan = fixtures.prepared_plan(source_sha256=hashlib.sha256(data).hexdigest(), source_bytes=len(data))
        artifact = self.artifact.read_bytes()
        plan.update(artifact_sha256=hashlib.sha256(artifact).hexdigest(), artifact_bytes=len(artifact))
        plan["plan_id"] = pdf_plan_identity(plan)
        checkout = checkout_snapshot(key, "1.99", pay_url="https://pay.example.invalid/existing",
                                     created_at=checkout_at or self.now - timedelta(minutes=5))
        values = {"input_path": str(self.source), "status": JobStatus.pending_payment,
                  "created_at": self.now - timedelta(days=2),
                  "translation_stats": {"attempt_id": "delivery", "pdf_conversion": fixtures.confirmed_plan(plan),
                                        "payment_checkout": checkout}}
        values.update(overrides)
        return fixtures.PdfStoreTests.job(self, key, **values)

    def check(self, job, **kwargs):
        return require_open_checkout([job], now=self.now, artifact_root=self.assets, **kwargs)

    def add(self, job):
        self.sql.add(job)
        return PersistentJobStore(self.engine).get(job.id)

    def trade(self, state="WAIT_BUYER_PAY", *, amount="1.99"):
        return {"out_trade_no": "book", "trade_status": state, "total_amount": amount}

    def test_pdf_clock_starts_at_checkout_after_long_preparation_and_survives_reload(self):
        job = self.add(self.job())
        before = deepcopy(job)
        self.assertLess(job.created_at.replace(tzinfo=timezone.utc), self.now - timedelta(days=1))
        self.assertEqual(checkout_created_at(job, now=self.now), self.now - timedelta(minutes=5))
        self.assertIsNone(self.check(job))
        self.assertEqual(PersistentJobStore(self.engine).get(job.id), before)

    def test_expired_pdf_checkout_is_not_revived_by_new_job_timestamp(self):
        job = self.job(checkout_at=self.now - timedelta(hours=3), created_at=self.now)
        with self.assertRaises(CheckoutUnavailable):
            self.check(job)

    def test_pdf_missing_malformed_naive_or_future_clock_never_falls_back(self):
        for value in (None, "", "bad", "2026-10-01T00:00:00", 123,
                      (self.now + timedelta(hours=1)).isoformat()):
            with self.subTest(value=type(value).__name__):
                job = self.job(created_at=self.now)
                job.translation_stats["payment_checkout"]["created_at"] = value
                with self.assertRaises(CheckoutUnavailable):
                    self.check(job)
        job = self.job()
        del job.translation_stats["payment_checkout"]
        with self.assertRaises(CheckoutUnavailable):
            self.check(job)

    def test_legacy_epub_clock_and_snapshot_shape_remain_unchanged(self):
        job = self.job(source_filename="ordinary.epub", input_path=str(self.root / "ordinary.epub"),
                       output_mode=OutputMode.simplified, translation_stats={}, created_at=self.now - timedelta(minutes=10))
        Path(job.input_path).write_bytes(b"epub fixture")
        saved = checkout_snapshot(job.id, job.expected_amount, pay_url="https://pay.example.invalid/existing")
        self.assertNotIn("created_at", saved)
        job.translation_stats["payment_checkout"] = {**saved, "created_at": "ignored legacy extra"}
        self.assertEqual(checkout_created_at(job), job.created_at)
        self.assertIsNone(self.check(job))
        job.created_at = self.now - timedelta(hours=3)
        with self.assertRaises(CheckoutUnavailable):
            self.check(job)

    def test_created_at_snapshot_requires_aware_datetime_and_preserves_utc_instant(self):
        for at in ("2026-10-01T00:00:00+00:00", self.now.replace(tzinfo=None), 123):
            with self.assertRaises(ValueError):
                checkout_snapshot("book", "1.99", pay_url="https://pay.example.invalid", created_at=at)
        offset = self.now.astimezone(timezone(timedelta(hours=8)))
        saved = checkout_snapshot("book", "1.99", pay_url="https://pay.example.invalid", created_at=offset)
        self.assertEqual(saved["created_at"], self.now.isoformat())

    def test_pdf_requires_original_channel_even_if_legacy_translation_flag_is_set(self):
        job = self.job(enable_translation=True)
        del job.translation_stats["payment_checkout"]
        with self.assertRaises(CheckoutUnavailable):
            original_checkout(job, job.id, "1.99")

    def test_changed_missing_or_symlink_source_stops_checkout_before_any_provider(self):
        job = self.job()
        original = self.source.read_bytes()
        self.source.write_bytes(original + b" changed")
        with self.assertRaises(CheckoutUnavailable):
            self.check(job)
        self.source.unlink()
        with self.assertRaises(CheckoutUnavailable):
            self.check(job)
        target = self.root / "target.pdf"
        target.write_bytes(original)
        self.source.symlink_to(target)
        with self.assertRaises(CheckoutUnavailable):
            self.check(job)
        self.query.assert_not_called()

    def test_changed_missing_or_symlink_prepared_artifact_stops_checkout(self):
        job = self.job()
        original = self.artifact.read_bytes()
        self.artifact.write_bytes(original + b" changed")
        with self.assertRaises(CheckoutUnavailable):
            self.check(job)
        self.artifact.unlink()
        with self.assertRaises(CheckoutUnavailable):
            self.check(job)
        target = self.root / "other.epub"
        target.write_bytes(original)
        self.artifact.symlink_to(target)
        with self.assertRaises(CheckoutUnavailable):
            self.check(job)

    def test_unconfirmed_missing_plan_and_artifact_root_mismatch_stop_checkout(self):
        for phase in ("preparing", "prepared", "missing"):
            job = self.job()
            if phase == "missing":
                del job.translation_stats["pdf_conversion"]
            elif phase == "preparing":
                current = job.translation_stats["pdf_conversion"]
                job.translation_stats["pdf_conversion"] = new_pdf_plan(current["source_sha256"], current["source_bytes"], current["amount"])
            else:
                plan = job.translation_stats["pdf_conversion"]
                plan["phase"] = "prepared"
                plan.pop("accepted_warnings")
                plan.pop("confirmed_at")
            with self.assertRaises(CheckoutUnavailable):
                self.check(job)
        with self.assertRaises(CheckoutUnavailable):
            require_open_checkout([self.job()], now=self.now, artifact_root=self.root / "wrong")

    def test_reconcile_never_closes_fresh_pdf_payment_based_on_old_upload(self):
        job = self.add(self.job())
        self.query.return_value = self.trade()
        self.assertEqual(reconcile.reconcile_payments.run(), {"checked": 1, "paid": 0, "closed": 0, "skipped": 1})
        self.close.assert_not_called()
        self.assertEqual(self.sql.get(job.id).status, JobStatus.pending_payment)

    def test_reconcile_foreign_or_corrupt_checkout_snapshot_cannot_supply_close_clock(self):
        for index, changed in enumerate(({"order_no": "other"}, {"amount": "0.01"},
                                         {"channel": "unknown"}, {"schema_version": 0})):
            key = f"invalid-{index}"
            job = self.job(key, checkout_at=self.now - timedelta(hours=3))
            job.translation_stats["payment_checkout"].update(changed)
            self.add(job)
        self.query.side_effect = lambda number: {"out_trade_no": number, "trade_status": "WAIT_BUYER_PAY", "total_amount": "1.99"}
        result = reconcile.reconcile_payments.run()
        self.assertEqual((result["checked"], result["skipped"], result["closed"]), (4, 4, 0))
        self.close.assert_not_called()
        self.dispatch.assert_not_called()

    def test_reconcile_invalid_pdf_clock_skips_close_but_verified_payment_still_wins(self):
        job = self.job()
        job.translation_stats["payment_checkout"].pop("created_at")
        self.add(job)
        self.query.return_value = self.trade()
        self.assertEqual(reconcile.reconcile_payments.run()["skipped"], 1)
        self.close.assert_not_called()
        self.query.return_value = self.trade("TRADE_SUCCESS")
        self.assertEqual(reconcile.reconcile_payments.run()["paid"], 1)
        self.assertEqual(self.sql.get(job.id).status, JobStatus.pending)
        self.assertEqual(self.sql.get(job.id).payment_resolution["state"], "paid")
        self.dispatch.assert_called_once_with(job.id, "delivery")

    def test_reconcile_expired_pdf_uses_verified_close_and_requeries_payment_race(self):
        job = self.add(self.job(checkout_at=self.now - timedelta(hours=3)))
        self.query.side_effect = [self.trade(), self.trade("TRADE_SUCCESS")]
        self.close.return_value = {"out_trade_no": job.id}
        result = reconcile.reconcile_payments.run()
        self.assertEqual((result["paid"], result["closed"]), (1, 0))
        self.close.assert_called_once_with(job.id)
        self.assertEqual(self.sql.get(job.id).status, JobStatus.pending)

    def test_reconcile_expired_pdf_closes_only_with_gateway_evidence(self):
        job = self.add(self.job(checkout_at=self.now - timedelta(hours=3)))
        self.query.side_effect = [self.trade(), self.trade("TRADE_CLOSED")]
        self.assertEqual(reconcile.reconcile_payments.run()["closed"], 1)
        self.assertEqual(self.sql.get(job.id).status, JobStatus.cancelled)
        self.assertEqual(self.sql.get(job.id).error_code, "PAYMENT_EXPIRED")
        self.dispatch.assert_not_called()

    def test_reconcile_missing_pdf_amount_never_uses_historical_translation_price(self):
        self.add(self.job(expected_amount=""))
        self.query.return_value = self.trade("TRADE_SUCCESS", amount="5.99")
        self.assertEqual(reconcile.reconcile_payments.run()["skipped"], 1)
        self.assertEqual(self.sql.get("book").status, JobStatus.pending_payment)
        self.dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
