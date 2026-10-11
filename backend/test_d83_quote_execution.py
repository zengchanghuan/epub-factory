"""Offline HTTP/worker checks of the original quote, without cache discounts."""
import copy
from dataclasses import replace
from contextlib import contextmanager
import os
import io
import zipfile
import unittest
from unittest.mock import patch

import test_d42_translation_contract as fixtures


class QuoteExecutionTests(unittest.TestCase):
    tearDown = fixtures.TranslationContractTests.tearDown
    _patch = fixtures.TranslationContractTests._patch
    payload = fixtures.TranslationContractTests.payload
    offline_preflight = fixtures.TranslationContractTests.offline_preflight
    create = fixtures.TranslationContractTests.create
    job_from = fixtures.TranslationContractTests.job_from
    confirm = fixtures.TranslationContractTests.confirm
    authorize = fixtures.TranslationContractTests.authorize
    confirmed_paid_job = fixtures.TranslationContractTests.confirmed_paid_job
    execute = fixtures.TranslationContractTests.execute

    def setUp(self):
        fixtures.TranslationContractTests.setUp(self)
        self.api.add_api_route('/api/v2/jobs/{job_id}/restart-translation',
                               self.main.restart_translation_v2, methods=['POST'])

    def alter_job(self, job, **fields):
        from app.storage_db import JobRecord
        with self.store._Session() as session:
            record = session.get(JobRecord, job.id)
            for key, value in fields.items(): setattr(record, key, value)
            session.commit()
        return self.store.get(job.id)

    def test_quote_binds_actual_deterministic_input_for_each_adapter(self):
        from app.domain.translation_input import normalized_translation_input
        from app.domain.translation_quote import validate_quote_execution
        for extension in ('.epub', '.docx', '.md'):
            job = self.confirmed_paid_job(extension)
            quote = copy.deepcopy(job.translation_stats['translation_pricing'])
            self.assertEqual(quote['execution_basis']['target_lang'], job.target_lang)
            for _ in range(2):
                with normalized_translation_input(job.input_path, source_name=job.source_filename) as normalized:
                    validate_quote_execution(job, normalized)
            finished, executor, _ = self.execute(job)
            executor.assert_called_once()
            self.assertEqual(finished.status, self.JobStatus.success, finished.message)
            self.assertEqual(finished.translation_stats['translation_pricing'], quote)

    def test_changed_price_defaults_and_model_defaults_do_not_reprice_or_reroute_paid_job(self):
        job = self.confirmed_paid_job()
        before = copy.deepcopy(job.translation_stats['translation_pricing'])
        with patch.dict(os.environ, {'OPENAI_MODEL': 'deepseek-flash', 'EPUB_DEFAULT_TRANSLATION_MODEL': 'deepseek-flash'}), \
             patch.object(self.main, '_calc_translation_price', side_effect=AssertionError('must not reprice')):
            finished, executor, _ = self.execute(job)
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)
        self.assertEqual(executor.call_args.kwargs['job'].translation_model, job.translation_model)
        self.assertEqual(finished.translation_stats['translation_pricing'], before)

    def test_worker_rejects_price_or_product_drift_before_executor(self):
        for fields in ({'translation_model': 'deepseek-flash'}, {'translation_quality': 'standard'},
                       {'target_lang': 'fr'}, {'expected_amount': '987.65'}):
            with self.subTest(fields=fields):
                job = self.confirmed_paid_job()
                before = copy.deepcopy(job.translation_stats['translation_pricing'])
                changed = self.alter_job(job, **fields)
                finished, executor, _ = self.execute(changed)
                executor.assert_not_called()
                self.assertEqual(finished.status, self.JobStatus.failed)
                self.assertIn('与原报价不一致', finished.message)
                self.assertEqual(finished.translation_stats['translation_pricing'], before)

    def test_adapter_version_drift_is_not_silently_overwritten(self):
        job = self.confirmed_paid_job('.docx')
        normalize = self.runner.normalized_translation_input
        @contextmanager
        def changed(*args, **kwargs):
            with normalize(*args, **kwargs) as normalized:
                yield replace(normalized, normalization_version='unexpected-new-adapter')
        with patch.object(self.runner, 'normalized_translation_input', changed):
            finished, executor, _ = self.execute(job)
        executor.assert_not_called()
        self.assertIn('与原报价不一致', finished.message)
        self.assertEqual(finished.expected_amount, job.expected_amount)

    def test_changed_normalized_body_with_unchanged_source_is_rejected(self):
        job = self.confirmed_paid_job('.md')
        from app.engine.adapters import html_to_epub_builder
        build = html_to_epub_builder.build
        def altered(body, metadata, path, **kwargs):
            return build(body + '<p>Unexpected added content.</p>', metadata, path, **kwargs)
        with patch.object(html_to_epub_builder, 'build', altered):
            finished, executor, _ = self.execute(job)
        executor.assert_not_called()
        self.assertEqual(finished.status, self.JobStatus.failed)
        self.assertIn('与原报价不一致', finished.message)

    def test_zip_container_changes_without_member_changes_do_not_invalidate_quote(self):
        job = self.confirmed_paid_job('.md')
        from app.engine.adapters import html_to_epub_builder
        build = html_to_epub_builder.build
        def repacked(body, metadata, path, **kwargs):
            build(body, metadata, path, **kwargs)
            with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as old:
                members = [(name, old.read(name)) for name in old.namelist()]
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as new:
                for name, data in reversed(members): new.writestr(name, data)
        with patch.object(html_to_epub_builder, 'build', repacked):
            finished, executor, _ = self.execute(job)
        executor.assert_called_once()
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)

    def test_flash_alias_is_not_a_product_upgrade(self):
        job = self.job_from(self.create(profile_confirmation='false', translation_model='deepseek-v4-flash'))
        job = self.authorize(job)
        job = self.alter_job(job, translation_model='deepseek-flash')
        finished, executor, _ = self.execute(job)
        executor.assert_called_once()
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)

    def test_retry_preserves_independent_nested_quote_and_allows_cache_policy_change(self):
        from app.domain.translation_attempt import restarted_translation_stats
        job = self.confirmed_paid_job()
        before = copy.deepcopy(job.translation_stats)
        new = restarted_translation_stats(before, attempt_id='next')
        new['translation_pricing']['execution_basis']['target_lang'] = 'fr'
        self.assertEqual(before, job.translation_stats)
        self.store.update_status(job.id, self.JobStatus.failed, 'interrupted')
        response = self.client.post('/api/v2/jobs/' + job.id + '/restart-translation?cache_policy=reuse',
            headers={'X-Job-Token': job.access_token})
        self.assertEqual(response.status_code, 200, response.text)
        refreshed = self.store.get(job.id)
        self.assertEqual(refreshed.cache_policy, 'reuse')
        self.assertEqual(refreshed.translation_stats['translation_pricing'], before['translation_pricing'])
        finished, executor, _ = self.execute(refreshed)
        executor.assert_called_once()
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)

    def test_legacy_order_without_execution_basis_keeps_original_behaviour_and_amount(self):
        job = self.confirmed_paid_job()
        old = copy.deepcopy(job.translation_stats)
        old['translation_pricing'].pop('execution_basis')
        self.store.update_status(job.id, self.JobStatus.pending, 'legacy', translation_stats=old)
        finished, executor, _ = self.execute(self.store.get(job.id))
        executor.assert_called_once()
        self.assertEqual(finished.status, self.JobStatus.success, finished.message)
        self.assertEqual(finished.expected_amount, job.expected_amount)
        self.assertNotIn('execution_basis', finished.translation_stats['translation_pricing'])

    def test_malformed_present_binding_does_not_fall_back_to_legacy(self):
        for bad in (None, {}, [], {'version': 999}):
            job = self.confirmed_paid_job()
            stats = copy.deepcopy(job.translation_stats)
            stats['translation_pricing']['execution_basis'] = bad
            self.store.update_status(job.id, self.JobStatus.pending, 'broken binding', translation_stats=stats)
            finished, executor, _ = self.execute(self.store.get(job.id))
            executor.assert_not_called()
            self.assertEqual(finished.status, self.JobStatus.failed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
