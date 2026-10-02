"""Opt-in real-book service-quote and immutable-order regression.

Inherits R1's four historical entitlement/download gates. New cases run the
real quote scanner and HTTP create/confirm lifecycle on the same three pinned
books. Profiling/payment creation are boundary stubs; no worker or model runs.
This proves pricing/authorization and unchanged artifact bytes, not translation
quality or a live payment. Customer sources are only read, never rewritten.
"""
from __future__ import annotations

import hashlib
import sqlite3
import unittest
import zipfile
from unittest.mock import patch

from bs4 import BeautifulSoup

import test_d37_entitlement_history as history


class HistoricalQuoteTests(history.HistoricalEntitlementTests):
    def setUp(self):
        super().setUp()
        self.forbidden_cache = self.patches.enter_context(patch(
            "app.engine.translation_cache.TranslationCache",
            side_effect=AssertionError("Service quoting must not inspect a runtime cache"),
        ))

    def tearDown(self):
        self.forbidden_cache.assert_not_called()
        super().tearDown()

    def upload(self, book, policy="reuse", *, confirmation=True):
        with (self.history_uploads / book["input"]).open("rb") as source:
            response = self.client.post("/api/v2/jobs", files={
                "file": (book["input"], source, "application/epub+zip"),
            }, data={
                "enable_translation": "true", "profile_confirmation": str(confirmation).lower(),
                "output_mode": "simplified", "translation_model": "deepseek-flash",
                "translation_quality": "standard", "cache_policy": policy,
            })
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        return self.store.get(result["job_id"]), {"X-Job-Token": result["access_token"]}, result

    def assert_full_service_quote(self, quote, policy):
        self.assertEqual(quote["schema_version"], 2)
        self.assertEqual(quote["quote_type"], "service_quote")
        self.assertEqual(quote["cache_policy"], policy)
        self.assertEqual(quote["cache_discount_status"], "disabled" if policy == "fresh" else "deferred")
        self.assertGreater(quote["total_chars"], 0)
        self.assertEqual(quote["billable_chars"], quote["total_chars"])
        self.assertEqual(quote["cached_chars"], 0)
        self.assertIsNone(quote["hit_ratio"])
        self.assertEqual(quote["price_cny"], quote["raw_price_cny"])
        self.assertEqual(quote["price_cny"], self.main._calc_translation_price(
            quote["total_chars"], "standard", "deepseek-flash"))

    def test_historical_fresh_reuse_and_verified_create_same_full_character_quote(self):
        from app.models import JobStatus
        for book in history.BOOKS:
            quotes = []
            for policy in ("fresh", "reuse", "verified"):
                with self.subTest(book=book["key"], policy=policy):
                    job, headers, response = self.upload(book, policy)
                    quote = response["pricing"]
                    self.assert_full_service_quote(quote, policy)
                    self.assertEqual(job.status, JobStatus.awaiting_confirmation)
                    self.assertEqual(job.translation_stats["translation_pricing"], quote)
                    self.assertEqual(response["amount"], quote["price_cny"])
                    self.assertEqual(job.expected_amount, quote["price_cny"])
                    self.assertEqual(job.payment_entitlement["amount"], quote["price_cny"])
                    detail = self.client.get(f"/api/v2/jobs/{job.id}", headers=headers)
                    self.assertEqual(detail.status_code, 200, detail.text)
                    self.assertEqual(detail.json()["pricing"], quote)
                    quotes.append((quote["total_chars"], quote["price_cny"]))
            self.assertEqual(len(set(quotes)), 1)
        self.payment.assert_not_called()
        self.enqueue.assert_not_called()

    def test_stale_legacy_cache_never_discounts_historical_checkout_or_changes_cache(self):
        # Deliberately populate the old language-only namespace, including error
        # responses that the old estimator treated as truthy cache hits. This is
        # a temporary DB; no actual customer cache is read or changed.
        cache_path = self.root / "pricing-cache.sqlite3"
        with sqlite3.connect(cache_path) as cache:
            cache.execute("CREATE TABLE IF NOT EXISTS translations (id TEXT PRIMARY KEY, source_html TEXT, translated_html TEXT, target_lang TEXT)")
            for book in history.BOOKS:
                with zipfile.ZipFile(self.history_uploads / book["input"]) as source:
                    for name in source.namelist():
                        if not name.lower().endswith((".xhtml", ".html", ".htm")):
                            continue
                        soup = BeautifulSoup(source.read(name).decode("utf-8", errors="ignore"), "html.parser")
                        tags = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]
                        for block in soup.find_all(tags):
                            if block.find(tags):
                                continue
                            inner = "".join(str(child) for child in block.contents).strip()
                            if inner:
                                key = hashlib.sha256((inner + "_zh-CN").encode("utf-8")).hexdigest()
                                cache.execute("INSERT OR REPLACE INTO translations VALUES (?, ?, ?, ?)",
                                              (key, inner, "ERROR: old provider failure", "zh-CN"))
            self.assertGreater(cache.execute("SELECT COUNT(*) FROM translations").fetchone()[0], 100)
        before = history.sha256(cache_path)
        for book in history.BOOKS:
            for policy in ("fresh", "reuse"):
                with self.subTest(book=book["key"], policy=policy):
                    self.payment.reset_mock()
                    job, _, response = self.upload(book, policy, confirmation=False)
                    self.assert_full_service_quote(response["pricing"], policy)
                    self.assertEqual(job.expected_amount, response["pricing"]["price_cny"])
                    self.payment.assert_called_once()
                    self.assertEqual(self.payment.call_args.kwargs["total_amount"], job.expected_amount)
        self.assertEqual(history.sha256(cache_path), before)
        self.enqueue.assert_not_called()

    def test_confirmation_edits_refresh_and_paid_retry_keep_original_quote(self):
        from app.domain.payment_entitlement import grant_verified_entitlement
        from app.models import ErrorCode, JobStatus
        from app.storage_db import PersistentJobStore
        for book in history.BOOKS:
            with self.subTest(book=book["key"]):
                self.payment.reset_mock()
                self.enqueue.reset_mock()
                job, headers, response = self.upload(book)
                quote, amount = response["pricing"], response["amount"]
                with patch.object(self.main, "_estimate_translation_pricing", side_effect=AssertionError("Existing quote must not be recomputed")):
                    with patch.object(self.main, "_calc_translation_price", side_effect=AssertionError("Existing quote must not use current tariff")):
                        confirmed = self.client.post(f"/api/v2/jobs/{job.id}/confirm-profile", headers=headers, json={
                            "translation_strategy": "academic_rigorous", "glossary": {"Evidence": "证据"},
                            "characters": [], "chapter_strategy_overrides": {}, "bilingual": True,
                        })
                        self.assertEqual(confirmed.status_code, 200, confirmed.text)
                        self.assertEqual(confirmed.json()["pricing"], quote)
                        self.assertEqual(confirmed.json()["amount"], amount)
                        saved = self.store.get(job.id)
                        self.assertEqual(saved.status, JobStatus.pending_payment)
                        self.assertEqual(saved.glossary["Evidence"], "证据")
                        self.assertEqual(saved.translation_strategy, "academic_rigorous")
                        self.assertEqual(saved.expected_amount, amount)
                        self.assertEqual(saved.payment_entitlement["amount"], amount)
                        self.payment.assert_called_once()
                        self.assertEqual(self.payment.call_args.kwargs["total_amount"], amount)
                        self.enqueue.assert_not_called()
                        # The existing R1 verified-payment boundary is simulated;
                        # no real gateway or worker executes in this suite.
                        grant_verified_entitlement(self.store, saved, amount, "verified_query")
                        self.assertTrue(self.store.try_mark_paid(job.id))
                        self.store.update_status(job.id, JobStatus.failed, "offline prior failure",
                                                 error_code=ErrorCode.PARTIAL_TRANSLATION.value)
                        paid = self.store.get(job.id)
                        reloaded = PersistentJobStore(engine=self.store._engine)
                        with patch.object(self.main, "job_store", reloaded):
                            detail = self.client.get(f"/api/v2/jobs/{job.id}", headers=headers)
                            self.assertEqual(detail.status_code, 200, detail.text)
                            self.assertEqual(detail.json()["pricing"], quote)
                            self.assertEqual(detail.json()["amount"], amount)
                            duplicate = self.client.post(f"/api/v2/jobs/{job.id}/confirm-profile", headers=headers, json={})
                            self.assertEqual(duplicate.status_code, 409)
                            retry = self.client.post(f"/api/v2/jobs/{job.id}/restart-translation", headers=headers)
                            self.assertEqual(retry.status_code, 200, retry.text)
                            final = reloaded.get(job.id)
                        self.assertEqual(final.expected_amount, amount)
                        self.assertEqual(final.translation_stats["translation_pricing"], quote)
                        self.assertEqual(final.payment_entitlement, paid.payment_entitlement)
                        self.payment.assert_called_once()
                        self.enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
