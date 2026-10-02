"""Cache-quote HTTP lifecycle contracts; reusable isolated R4 fixtures only."""
import copy
import unittest
from unittest.mock import patch

import test_d42_translation_contract as fixtures


class QuoteContractTests(unittest.TestCase):
    setUp = fixtures.TranslationContractTests.setUp
    tearDown = fixtures.TranslationContractTests.tearDown
    _patch = fixtures.TranslationContractTests._patch
    payload = fixtures.TranslationContractTests.payload
    offline_preflight = fixtures.TranslationContractTests.offline_preflight
    create = fixtures.TranslationContractTests.create
    job_from = fixtures.TranslationContractTests.job_from
    confirm = fixtures.TranslationContractTests.confirm
    authorize = fixtures.TranslationContractTests.authorize

    def test_create_passes_each_policy_and_persists_honest_quote_without_cache_reads(self):
        with patch.object(self.cache, "get", side_effect=AssertionError("obsolete cache quote")), \
             patch.object(self.cache, "get_latest_compatible", side_effect=AssertionError("compatible quote")):
            for policy in ("fresh", "verified", "reuse"):
                response = self.create(cache_policy=policy)
                job = self.job_from(response)
                quote = response.json()["pricing"]
                self.assertEqual(quote["schema_version"], 2)
                self.assertEqual(quote["cache_policy"], policy)
                self.assertEqual(quote["cache_discount_status"], "disabled" if policy == "fresh" else "deferred")
                self.assertIsNone(quote["hit_ratio"])
                self.assertEqual(quote["cached_chars"], 0)
                self.assertEqual(quote["billable_chars"], quote["total_chars"])
                self.assertEqual(quote["price_cny"], job.expected_amount)
                self.assertEqual(quote, job.translation_stats["translation_pricing"])
                self.assertEqual(job.payment_entitlement["amount"], job.expected_amount)
        self.page_pay.assert_not_called()
        self.enqueue.assert_not_called()

    def test_confirmation_edits_context_without_repricing_or_mutating_quote(self):
        job = self.job_from(self.create(cache_policy="verified"))
        quote = copy.deepcopy(job.translation_stats["translation_pricing"])
        amount = job.expected_amount
        with patch.object(self.main, "_estimate_translation_pricing", side_effect=AssertionError("must not reprice")):
            response = self.confirm(job, translation_strategy="mirror_fidelity", glossary={"liberty": "自主"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["pricing"], quote)
        self.assertEqual(response.json()["amount"], amount)
        saved = self.store.get(job.id)
        self.assertEqual(saved.expected_amount, amount)
        self.assertEqual(saved.payment_entitlement["amount"], amount)
        self.assertEqual(saved.translation_strategy, "mirror_fidelity")
        self.assertEqual(self.page_pay.call_args.kwargs["total_amount"], amount)

    def test_refresh_and_store_reload_keep_original_amount_and_pricing(self):
        job = self.job_from(self.create(profile_confirmation="false", cache_policy="reuse"))
        with patch.object(self.main, "job_store", self.PersistentJobStore(self.engine)), \
             patch.object(self.main, "_estimate_translation_pricing", side_effect=AssertionError("refresh repriced")):
            response = self.client.get("/jobs/" + job.id, headers={"X-Job-Token": job.access_token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["amount"], job.expected_amount)
        self.assertEqual(response.json()["pricing"], job.translation_stats["translation_pricing"])

    def test_retry_resets_execution_counters_but_not_original_quote(self):
        from app.domain.translation_attempt import restarted_translation_stats
        job = self.job_from(self.create())
        old = copy.deepcopy(job.translation_stats)
        old.update(api_calls=17, cached_chunks=100)
        new = restarted_translation_stats(old, attempt_id="offline-retry")
        self.assertEqual(new["api_calls"], 0)
        self.assertEqual(new["cached_chunks"], 0)
        self.assertEqual(new["translation_pricing"], old["translation_pricing"])
        self.assertIsNot(new["translation_pricing"], old["translation_pricing"])

    def test_old_order_without_v2_quote_retains_amount_and_does_not_invent_cache_data(self):
        from app.models import Job, JobStatus, OutputMode
        job = Job(id="legacy-priced-order", trace_id="legacy", source_filename="legacy.epub",
                  input_path="unused", output_mode=OutputMode.simplified, access_token="legacy-token",
                  expected_amount="12.34", enable_translation=True, status=JobStatus.pending_payment)
        self.store.add(job)
        with patch.object(self.main, "_estimate_translation_pricing", side_effect=AssertionError("legacy repriced")):
            response = self.client.get("/jobs/" + job.id, headers={"X-Job-Token": job.access_token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["amount"], "12.34")
        self.assertIsNone(response.json()["pricing"])
        self.assertEqual(self.store.get(job.id).expected_amount, "12.34")

    def test_quote_read_error_fails_before_profile_payment_or_order(self):
        from app.domain.translation_quote import QuoteInputError
        with patch("app.domain.translation_quote.estimate_quote", side_effect=QuoteInputError("damaged resource")):
            response = self.create()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("未创建支付订单", response.json()["detail"])
        self.assertEqual(self.store.list_jobs(), [])
        self.page_pay.assert_not_called()
        self.preflight.assert_not_called()
        self.enqueue.assert_not_called()

    def test_bad_pricing_configuration_is_not_reported_as_a_damaged_customer_book(self):
        with patch.object(self.main, "_calc_translation_price", side_effect=ValueError("invalid price config")):
            response = self.create()
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("检查文件", response.json()["detail"])
        self.assertEqual(self.store.list_jobs(), [])
        self.page_pay.assert_not_called()
        self.preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
