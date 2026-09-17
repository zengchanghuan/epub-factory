"""Offline regressions for the 88c8dc5ba770 order; never calls a model or broker.

Set EPUB_REGRESSION_BOOK to the downloaded original to run the real-book cases.
"""
import asyncio
import hashlib
import json
import os
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.domain.chapter_reduce_service import apply_chunk_results
from app.domain.manifest_service import build_manifest
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_qa_service import audit_translated_epub_output, build_translation_qa_report
from app.domain.fast_translation_runner import _translate_manifest_async
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.infra.celery_app import build_celery_app
from app.infra.execution_lease import (RedisExecutionLease, execution_lease,
                                      ExecutionLeaseLost, ExecutionLeaseUnavailable, ExecutionLeaseBusy)
from app.infra.llm_errors import ProviderAccountUnavailable
from app.models import ErrorCode, Job, JobStatus, OutputMode
from app.storage import JobStore
from app.job_runner import run_job

TITLE = 'LUKAS FRÖHLICH'
TITLE_HTML = f'<h3 id="id__798_35_">{TITLE}</h3>'
# Controlled response for pipeline testing, not a verified editorial name.
CHINESE_TITLE = f'卢卡斯·弗勒利希（{TITLE}）'
BOOK = os.environ.get('EPUB_REGRESSION_BOOK', '')
SNAPSHOTS = os.environ.get('EPUB_REGRESSION_SNAPSHOTS', '')
SHA = 'b5bcf437126c888f5f092a16590cdbfdf1d3ec2d787acf0ec7187536b6a709c4'


class BalanceError(RuntimeError):
    status_code = 402


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.renewals = 0

    def set(self, key, owner, nx, ex):
        if key in self.values:
            return False
        self.values[key] = owner
        return True

    def get(self, key):
        return self.values.get(key)

    def eval(self, script, count, key, owner, *args):
        if self.values.get(key) != owner:
            return 0
        if args:
            self.renewals += 1
        else:
            del self.values[key]
        return 1


def translator(**kwargs):
    t = SemanticsTranslator(target_lang='zh-CN', **kwargs)
    t.cache = Mock(get=Mock(return_value=None), get_latest_compatible=Mock(return_value=None))
    return t


def final_audit(text, preserved_terms=()):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'diagnostic.epub'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('chapter.xhtml', f'<html><body><h3>{text}</h3></body></html>')
        return audit_translated_epub_output(path, preserved_terms=preserved_terms)


class TestDeliveryRecovery(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'CELERY_BROKER_URL': '', 'REDIS_URL': '', 'OPENAI_API_KEY': 'offline-test',
            'EPUB_LLM_RATE_LIMITER_ENABLED': '0', 'EPUB_LLM_GLOBAL_HEALTH_ENABLED': '0',
            'EPUB_BOOK_PROFILER_ENABLED': '0',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_visibility_exceeds_book_hard_limit_on_all_configs(self):
        with patch.dict(os.environ, {}, clear=True):
            app = build_celery_app()
        self.assertEqual(app.conf.broker_transport_options['visibility_timeout'], 10800)
        self.assertEqual(app.conf.result_backend_transport_options['visibility_timeout'], 10800)
        self.assertEqual(app.conf.visibility_timeout, 10800)
        self.assertGreater(app.conf.visibility_timeout, app.conf.task_annotations['jobs.run_conversion']['time_limit'])

    def test_unsafe_visibility_timeout_is_rejected(self):
        with patch.dict(os.environ, {'CELERY_VISIBILITY_TIMEOUT': '3600'}):
            with self.assertRaises(ValueError):
                build_celery_app()

    def test_redis_lease_excludes_duplicate_and_renews(self):
        client = FakeRedis()
        first = RedisExecutionLease(client, 'order-attempt')
        second = RedisExecutionLease(client, 'order-attempt')
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        self.assertTrue(first.renew())
        self.assertEqual(client.renewals, 1)
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_old_owner_cannot_renew_or_release_new_owner(self):
        client = FakeRedis()
        lease = RedisExecutionLease(client, 'order-attempt')
        lease.acquire()
        client.values[lease.key] = 'new-owner'
        self.assertFalse(lease.renew())
        with self.assertRaises(ExecutionLeaseLost):
            lease.assert_owned()
        lease.release()
        self.assertEqual(client.values[lease.key], 'new-owner')

    def test_owner_check_detects_loss_before_heartbeat(self):
        client = FakeRedis()
        lease = RedisExecutionLease(client, 'order-attempt')
        lease.acquire()
        del client.values[lease.key]
        with self.assertRaises(ExecutionLeaseLost):
            lease.assert_owned()

    def test_redis_outage_fails_closed(self):
        client = Mock(set=Mock(side_effect=ConnectionError('offline')))
        with self.assertRaises(ExecutionLeaseUnavailable):
            RedisExecutionLease(client, 'order-attempt').acquire()

    def test_local_lease_excludes_duplicates_and_releases(self):
        key = uuid.uuid4().hex
        with execution_lease(key, 'attempt') as first:
            self.assertIsNotNone(first)
            with execution_lease(key, 'attempt') as duplicate:
                self.assertIsNone(duplicate)
        with execution_lease(key, 'attempt') as next_executor:
            self.assertIsNotNone(next_executor)

    def make_job(self, status=JobStatus.pending):
        return Job(id=uuid.uuid4().hex, source_filename='book.epub', trace_id='offline',
                   input_path='/missing-test-book.epub', output_mode=OutputMode.simplified,
                   enable_translation=True, status=status,
                   translation_stats={'attempt_id': 'attempt', 'translated_chunks': 7})

    def test_terminal_deliveries_do_not_convert_or_clear_progress(self):
        for status in [JobStatus.success, JobStatus.failed, JobStatus.cancelled, JobStatus.pending_payment]:
            job = self.make_job(status)
            store = JobStore()
            store.add(job)
            with patch('app.job_runner.job_store', store), \
                 patch('app.domain.fast_translation_runner.run_fast_translation_job') as convert:
                run_job(job.id, 'attempt')
            convert.assert_not_called()
            self.assertEqual(job.translation_stats['translated_chunks'], 7)
            self.assertEqual(job.status, status)

    def test_stale_attempt_does_not_convert(self):
        job = self.make_job()
        store = JobStore()
        store.add(job)
        with patch('app.job_runner.job_store', store), \
             patch('app.domain.fast_translation_runner.run_fast_translation_job') as convert:
            run_job(job.id, 'old-attempt')
        convert.assert_not_called()
        self.assertEqual(job.status, JobStatus.pending)

    def test_duplicate_delivery_only_enters_runner_once(self):
        job = self.make_job()
        store = JobStore()
        store.add(job)
        def execute(*args):
            run_job(job.id, 'attempt')
        with patch('app.job_runner.job_store', store), \
             patch('app.job_runner._run_job_locked', side_effect=execute) as execute_mock:
            run_job(job.id, 'attempt')
        self.assertEqual(execute_mock.call_count, 1)

    def test_broker_busy_lease_is_retried_not_dropped(self):
        job = self.make_job()
        store = JobStore()
        store.add(job)
        with patch('app.job_runner.job_store', store), execution_lease(job.id, 'attempt'), \
             patch('app.job_runner._run_job_locked') as execute:
            with self.assertRaises(ExecutionLeaseBusy):
                run_job(job.id, 'attempt', retry_if_busy=True)
        execute.assert_not_called()
        self.assertEqual(job.translation_stats['translated_chunks'], 7)

    def test_provider_pause_keeps_progress_and_has_operational_error(self):
        job = self.make_job()
        store = JobStore()
        store.add(job)
        with patch('app.job_runner.job_store', store), \
             patch('app.domain.fast_translation_runner.run_fast_translation_job',
                   side_effect=ProviderAccountUnavailable('deepseek')), \
             patch('app.job_runner.report_error'), patch('app.job_runner.notify_job_completed'):
            run_job(job.id, 'attempt')
        self.assertEqual(job.status, JobStatus.failed)
        self.assertEqual(job.error_code, ErrorCode.TRANSLATION_PROVIDER_UNAVAILABLE)
        self.assertEqual(job.translation_stats['translated_chunks'], 7)
        self.assertEqual(job.translation_stats['qa_report']['status'], 'blocked')
        self.assertIsNone(job.translation_stats['qa_report']['score'])
        self.assertNotIn('质检未通过', job.message)

    def test_name_rejection_is_consistent_across_three_layers(self):
        self.assertTrue(translator()._looks_untranslated(TITLE_HTML, TITLE))
        self.assertTrue(audit_translation_chunk(original_html=TITLE_HTML, translated_html=TITLE).likely_untranslated)
        self.assertEqual(final_audit(TITLE)['status'], 'failed')

    def test_chinese_name_with_original_passes_all_three_layers(self):
        self.assertFalse(translator()._looks_untranslated(TITLE_HTML, CHINESE_TITLE))
        self.assertFalse(audit_translation_chunk(original_html=TITLE_HTML, translated_html=CHINESE_TITLE).likely_untranslated)
        self.assertEqual(final_audit(CHINESE_TITLE)['status'], 'passed')

    def test_explicit_preservation_is_exact_not_all_titles(self):
        t = translator(glossary={TITLE: TITLE})
        self.assertFalse(t._looks_untranslated(TITLE_HTML, TITLE))
        self.assertFalse(audit_translation_chunk(original_html=TITLE_HTML, translated_html=TITLE,
                                                preserved_terms=t.preserved_terms).likely_untranslated)
        self.assertEqual(final_audit(TITLE, t.preserved_terms)['status'], 'passed')
        self.assertEqual(final_audit('The Evolution of Trading', t.preserved_terms)['status'], 'failed')
        self.assertTrue(t._looks_untranslated('<h3>The Evolution of Trading</h3>', 'The Evolution of Trading'))

    def test_unconfirmed_automatic_name_is_not_preserved(self):
        t = translator(glossary={TITLE: TITLE}, preserved_terms=[])
        self.assertTrue(t._looks_untranslated(TITLE_HTML, TITLE))

    def test_cache_revalidates_only_invalid_entry(self):
        t = translator()
        t.cache.get.return_value = TITLE
        self.assertIsNone(t._cache_get(TITLE))
        t.cache.get.return_value = CHINESE_TITLE
        self.assertEqual(t._cache_get(TITLE), CHINESE_TITLE)
        t._cache_set(TITLE, TITLE)
        t.cache.set.assert_not_called()

    def test_title_is_repaired_instead_of_reported_success_unchanged(self):
        t = translator()
        calls = []
        async def responses(payload, **kwargs):
            calls.append(payload)
            return ({item['id']: TITLE if len(calls) == 1 else CHINESE_TITLE for item in payload},
                    {'model': 'offline', 'attempts': 1})
        with patch.object(t, '_call_llm_json_batch', new=responses), \
             patch('app.engine.cleaners.semantics_translator.asyncio.sleep', new=AsyncMock()):
            result = asyncio.run(t.translate_many_chunks_async([TITLE_HTML]))[0]
        self.assertIsNone(result.error)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.translated_html, CHINESE_TITLE)

    def test_balance_error_does_not_split_batch_or_rescue_chunks(self):
        t = translator()
        request = AsyncMock(side_effect=BalanceError('Insufficient Balance'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        with patch.object(t, '_get_client', return_value=client), \
             patch.object(t, '_candidate_routes', return_value=[('https://mock.invalid', 'a'), ('https://mock.invalid', 'b')]):
            with self.assertRaises(ProviderAccountUnavailable):
                asyncio.run(t.translate_many_chunks_async([TITLE_HTML] * 10))
            self.assertEqual(request.await_count, 1)
            with self.assertRaises(ProviderAccountUnavailable):
                asyncio.run(t.translate_many_chunks_async([TITLE_HTML]))
            self.assertEqual(request.await_count, 1)
        self.assertEqual(t.stats.batch_splits, 0)
        self.assertEqual(t.stats.chunk_rescue_attempts, 0)

    def test_successful_response_is_cached_before_later_balance_pause(self):
        t = translator()
        t.adaptive_batch_max_chars = 1000
        good = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({'results': [{'id': 0, 'translation': CHINESE_TITLE}]})))], usage=None)
        request = AsyncMock(side_effect=[good, BalanceError('Insufficient Balance')])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        long_html = '<p>' + ('This long paragraph describes the trading strategy in detail. ' * 40) + '</p>'
        with patch.dict(os.environ, {'OPENAI_CONCURRENCY': '1'}), \
             patch.object(t, '_get_client', return_value=client), \
             patch.object(t, '_candidate_routes', return_value=[('https://mock.invalid', 'a')]):
            with self.assertRaises(ProviderAccountUnavailable):
                asyncio.run(t.translate_many_chunks_async([TITLE_HTML, long_html]))
        self.assertEqual(request.await_count, 2)
        t.cache.set.assert_called_once()
        self.assertEqual(t.cache.set.call_args.args[1], CHINESE_TITLE)

    def test_balance_error_switches_only_to_independent_configured_account(self):
        t = translator()
        primary, backup = 'https://primary.invalid', 'https://backup.invalid'
        t._route_provider_by_base_url = {primary: 'deepseek', backup: 'aliyun'}
        t._route_api_key_by_base_url = {primary: 'account-a', backup: 'account-b'}
        failed = AsyncMock(side_effect=BalanceError('Insufficient Balance'))
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({'results': [{'id': 0, 'translation': CHINESE_TITLE}]})))], usage=None)
        success = AsyncMock(return_value=response)
        def client(base):
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=failed if base == primary else success)))
        with patch.object(t, '_get_client', side_effect=client), \
             patch.object(t, '_candidate_routes', return_value=[(primary, 'a'), (primary, 'b'), (backup, 'a')]):
            result = asyncio.run(t.translate_many_chunks_async([TITLE_HTML]))[0]
        self.assertIsNone(result.error)
        self.assertEqual(failed.await_count, 1)
        self.assertEqual(success.await_count, 1)

    def test_paused_report_does_not_blame_untranslated_content(self):
        qa = build_translation_qa_report(translation_stats={
            'failed_chunks': 6, 'audit_failed_chunks': 6,
            'audit_flags_count': {'likely_untranslated': 6}, 'provider_blocked': True,
            'last_error': '模型服务余额不足'}, error_code='TRANSLATION_PROVIDER_UNAVAILABLE')
        self.assertEqual(qa['status'], 'blocked')
        self.assertEqual(qa['flags'], ['provider_unavailable'])

    def test_paused_task_can_use_cache_reusing_retry_endpoint(self):
        from app.main import retry_translation_v2
        job = self.make_job(JobStatus.failed)
        job.error_code = ErrorCode.TRANSLATION_PROVIDER_UNAVAILABLE.value
        store = JobStore()
        store.add(job)
        with patch('app.main.job_store', store), patch('app.main._authorize_job_access', return_value=True), \
             patch('app.main._restart_translation_job', return_value={'status': 'queued'}) as restart:
            result = retry_translation_v2(job.id, Mock(), Mock())
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(restart.call_args.kwargs['cache_policy'], 'reuse')

    def test_historical_balance_errors_are_not_diagnosed_as_content_errors(self):
        from app.main import _diagnose_error_category
        self.assertEqual(_diagnose_error_category('Error code: 402 - Insufficient Balance',
                                                  ['likely_untranslated']), 'provider_balance')

    def test_balance_block_is_shared_by_same_host_and_key_not_model_or_path(self):
        t = translator()
        self.assertEqual(t._account_key('https://api.deepseek.com/v1'),
                         t._account_key('https://api.deepseek.com'))
        t._route_api_key_by_base_url['https://api.deepseek.com/v1'] = 'different-account'
        self.assertNotEqual(t._account_key('https://api.deepseek.com/v1'),
                            t._account_key('https://api.deepseek.com'))

    def test_all_quality_defaults_prefer_flash_but_explicit_pro_is_preserved(self):
        from app.main import _normalize_translation_model
        with patch('app.main.DEFAULT_TRANSLATION_MODEL', 'deepseek-v4-flash'):
            for quality in ['standard', 'high', 'literary']:
                self.assertEqual(_normalize_translation_model(None, True, translation_quality=quality),
                                 'deepseek-v4-flash')
                self.assertEqual(_normalize_translation_model('deepseek-v4-pro', True, translation_quality=quality),
                                 'deepseek-v4-pro')

    def test_complex_chunk_first_pass_does_not_silently_upgrade_to_pro(self):
        t = translator(model='deepseek-v4-flash')
        calls = []
        source = '<p>Watson wrote the note.<sup><a id="fn1"></a><a href="notes.xhtml#n1">1</a></sup></p>'
        async def response(payload, **kwargs):
            calls.append(kwargs.get('preferred_model'))
            return ({0: '沃森写下了这条注释。<sup><a id="fn1"></a><a href="notes.xhtml#n1">1</a></sup>'},
                    {'model': 'deepseek-v4-flash', 'attempts': 1})
        with patch.dict(os.environ, {'EPUB_TRANSLATION_PROACTIVE_QUALITY_MODEL_ENABLED': '0'}), \
             patch.object(t, '_call_llm_json_batch', new=response):
            result = asyncio.run(t.translate_many_chunks_async([source]))[0]
        self.assertIsNone(result.error)
        self.assertEqual(calls, [None])
        self.assertEqual(t.stats.proactive_quality_routes, 0)

    def test_glossary_balance_failure_stops_remaining_batches(self):
        from app.engine.glossary_extractor import GlossaryCandidate, translate_glossary
        request = AsyncMock(side_effect=BalanceError('Insufficient Balance'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        candidates = [GlossaryCandidate(term=f'Trader {index}', count=1) for index in range(3)]
        with patch.dict(os.environ, {'OPENAI_MODEL': 'deepseek-v4-flash', 'OPENAI_API_KEY': 'offline-test'}), \
             patch('openai.AsyncOpenAI', return_value=client):
            with self.assertRaises(ProviderAccountUnavailable):
                asyncio.run(translate_glossary(candidates, max_terms_per_call=1))
        self.assertEqual(request.await_count, 1)

    def test_map_pause_cancels_siblings_and_publishes_operational_stats(self):
        job = self.make_job()
        store = JobStore()
        store.add(job)
        manifest = {'chapters': [{'chapter_id': 'c1', 'file_path': 'c1.xhtml', 'chapter_kind': 'body',
                                 'chunks': [{'chunk_id': 'c1_0', 'html': TITLE_HTML, 'text': TITLE,
                                             'sequence': 0, 'locator': '/html[1]/body[1]/h3[1]'}]}]}
        async def paused(*args, **kwargs):
            raise ProviderAccountUnavailable('deepseek')
        with patch('app.domain.fast_translation_runner.job_store', store), \
             patch.object(SemanticsTranslator, 'translate_many_chunks_async', new=paused):
            with self.assertRaises(ProviderAccountUnavailable):
                asyncio.run(_translate_manifest_async(job=job, manifest=manifest,
                    content_by_file={}, glossary={}, progress_callback=lambda msg: None))
        self.assertTrue(job.translation_stats['provider_blocked'])
        self.assertEqual(job.translation_stats['provider_error'], 'insufficient_balance')

    def test_chinese_source_with_foreign_names_is_not_untranslated(self):
        text = '这位交易员名叫 LUKAS FRÖHLICH，文中保留其外文姓名。'
        self.assertFalse(translator()._looks_untranslated(text, text))
        self.assertFalse(audit_translation_chunk(original_html=text, translated_html=text).likely_untranslated)

    @unittest.skipUnless(BOOK, 'EPUB_REGRESSION_BOOK not provided')
    def test_real_original_manifest_and_title_recovery(self):
        path = Path(BOOK)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), SHA)
        manifest = build_manifest(str(path), 'offline-regression')
        self.assertFalse(manifest.get('error'))
        self.assertEqual(sum(len(c['chunks']) for c in manifest['chapters'] if c['chapter_kind'] == 'body'), 2665)
        chapter = next(c for c in manifest['chapters'] if c['chapter_id'] == 'c179')
        title = next(c for c in chapter['chunks'] if c['chunk_id'] == 'c179_0002')
        self.assertEqual(title['text'], TITLE)
        raw = ('<html><body>' + title['html'] + '</body></html>').encode()
        result = SimpleNamespace(locator=title['locator'], sequence=title['sequence'],
                                 chunk_id=title['chunk_id'], translated_html=CHINESE_TITLE)
        output = apply_chunk_results(raw, [result], bilingual=False)
        self.assertIn('id__798_35_', output.decode())
        self.assertFalse(audit_translation_chunk(original_html=title['html'],
                                                translated_html=CHINESE_TITLE).likely_untranslated)
        self.assertEqual(final_audit(CHINESE_TITLE)['status'], 'passed')

    @unittest.skipUnless(SNAPSHOTS, 'EPUB_REGRESSION_SNAPSHOTS not provided')
    def test_real_saved_chapters_only_flag_the_known_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'partial-diagnostic.epub'
            with zipfile.ZipFile(path, 'w') as archive:
                for chapter in sorted(Path(SNAPSHOTS).glob('*.xhtml')):
                    archive.write(chapter, chapter.name)
            audit = audit_translated_epub_output(path)
        self.assertEqual(audit['html_files'], 11)
        self.assertEqual(audit['checked_text_blocks'], 1548)
        self.assertEqual(audit['residual_blocks'], 1)
        self.assertEqual(audit['samples'][0]['snippet'], TITLE)


if __name__ == '__main__':
    unittest.main()
