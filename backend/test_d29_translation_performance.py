"""Offline performance/resume regressions: no model, broker or production writes."""
import asyncio
import copy
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import uuid
import zipfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ebooklib import epub
from bs4 import BeautifulSoup
from billiard.exceptions import SoftTimeLimitExceeded
from app.cancellation import JobCancelled
from app.domain.fast_translation_runner import _translate_manifest_async, run_fast_translation_job
from app.domain.manifest_service import build_manifest
from app.domain.epub_navigation_audit import audit_epub_navigation
from app.domain.translation_residual_policy import normalize_text, residual_category
from app.domain.translation_attempt import restarted_translation_stats
from app.domain.translation_checkpoints import TranslationCheckpoints, book_resume_key
from app.engine.cleaners.semantics_translator import SemanticsTranslator, SingleChunkResult, _within_request_budget
from app.engine.glossary_extractor import GlossaryCandidate, translate_glossary
from app.engine.glossary_service import GlossaryBuildResult
from app.engine.translation_cache import TranslationCache
from app.engine.chunk_extractor import is_external_text_node
from app.infra.async_requests import bounded_request, gather_cancel_on_error
from app.infra.llm_errors import ProviderAccountUnavailable
from app.infra.llm_guard import ModelNotAllowedError
from app.models import Job, JobStatus, OutputMode, ConversionResult
from app.storage import JobStore

ALPHA = 'Alpha proposes this approach.'
BETA = 'Beta supports this approach.'
TRANSLATIONS = {ALPHA: '阿尔法提出这种方法。', BETA: '贝塔支持这种方法。'}


def manifest_for(*texts):
    return {'chapters': [{'chapter_id': f'c{i}', 'file_path': f'c{i}.xhtml', 'chapter_kind': 'body',
                         'chunks': [{'chunk_id': f'c{i}_0', 'sequence': 0, 'locator': '/html[1]/body[1]/p[1]',
                                     'html': f'<p>{text}</p>', 'text': text, 'translation_strategy': 'html'}]}
                        for i, text in enumerate(texts)]}


def reply(mapping):
    return mapping, {'model': 'deepseek-flash', 'base_url': 'https://mock.invalid', 'attempts': 1}


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'checkpoints.db')
        env = patch.dict(os.environ, {
            'OPENAI_API_KEY': 'offline-test', 'OPENAI_MODEL': 'deepseek-flash',
            'EPUB_DEFAULT_TRANSLATION_MODEL': 'deepseek-flash', 'CELERY_BROKER_URL': '', 'REDIS_URL': '',
            'EPUB_LLM_RATE_LIMITER_ENABLED': '0', 'EPUB_LLM_GLOBAL_HEALTH_ENABLED': '0',
            'EPUB_BOOK_PROFILER_ENABLED': '0', 'EPUB_TRANSLATION_CHECKPOINT_DB': self.db,
            'EPUB_GLOSSARY_CONCURRENCY': '2', 'EPUB_TRANSLATION_QUALITY_RETRIES': '1',
            'EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE': '0',
        })
        env.start(); self.addCleanup(env.stop)
        cache = patch('app.engine.cleaners.semantics_translator.TranslationCache',
                      return_value=Mock(get=Mock(return_value=None), get_latest_compatible=Mock(return_value=None)))
        cache.start(); self.addCleanup(cache.stop)
        self.store = JobStore()
        store = patch('app.domain.fast_translation_runner.job_store', self.store)
        store.start(); self.addCleanup(store.stop)
        self.job = Job(id=uuid.uuid4().hex, source_filename='book.epub', trace_id='offline',
                       input_path='/offline.epub', output_mode=OutputMode.simplified,
                       enable_translation=True, status=JobStatus.running,
                       translation_stats={'attempt_id': 'attempt-1'})
        self.store.add(self.job)

    def run_manifest(self, manifest, content_by_file=None):
        if content_by_file is None:
            content_by_file = {ch['file_path']: ('<html><body>' + ''.join(c['html'] for c in ch['chunks']) + '</body></html>').encode()
                               for ch in manifest['chapters']}
        return asyncio.run(_translate_manifest_async(job=self.job, manifest=manifest, glossary={},
            content_by_file=content_by_file, progress_callback=lambda _: None))

    def test_checkpoint_is_durable_and_book_scoped(self):
        first = TranslationCheckpoints('a', 'scope', self.db)
        first.put('chunk:x', {'result': 'saved'})
        self.assertEqual(TranslationCheckpoints('a', 'scope', self.db).chunks(), {'x': {'result': 'saved'}})
        self.assertEqual(TranslationCheckpoints('b', 'scope', self.db).chunks(), {})
        self.assertIsNone(TranslationCheckpoints('a', 'other', self.db).get('chunk:x'))

    def test_resume_key_invalidates_source_and_user_settings_not_attempt(self):
        manifest = manifest_for(ALPHA)
        key = book_resume_key(self.job, manifest)
        self.job.translation_stats['attempt_id'] = 'attempt-2'
        self.assertEqual(key, book_resume_key(self.job, manifest))
        for field, value in [('translation_model', 'deepseek-v4-pro'), ('target_lang', 'fr'),
                             ('glossary', {'Alpha': '阿尔法'}), ('temperature', .6),
                             ('translation_quality', 'high'), ('bilingual', True)]:
            changed = copy.deepcopy(self.job); setattr(changed, field, value)
            self.assertNotEqual(key, book_resume_key(changed, manifest), field)
        changed = copy.deepcopy(manifest); changed['chapters'][0]['chunks'][0]['html'] += 'changed'
        self.assertNotEqual(key, book_resume_key(self.job, changed))

    def test_fresh_attempt_isolated_but_same_attempt_can_resume(self):
        self.job.cache_policy = 'fresh'
        manifest = manifest_for(ALPHA)
        key = book_resume_key(self.job, manifest)
        self.assertEqual(key, book_resume_key(self.job, manifest))
        self.job.translation_stats['attempt_id'] = 'attempt-2'
        self.assertNotEqual(key, book_resume_key(self.job, manifest))

    def test_retry_preserves_user_confirmation_not_runtime_counters(self):
        preflight = {'confirmed': True, 'resolved_strategy': 'academic_rigorous', 'version': 7}
        result = restarted_translation_stats({'translation_preflight': preflight, 'api_calls': 20}, attempt_id='new')
        self.assertEqual(result['translation_preflight'], preflight)
        self.assertEqual(result['api_calls'], 0)
        self.assertNotIn('translation_preflight', restarted_translation_stats(
            {'translation_preflight': {'confirmed': False}}, attempt_id='new'))

    def test_compatible_cache_uses_source_index(self):
        cache = TranslationCache(str(Path(self.tmp.name) / 'translations.db'))
        cache.set('source', '旧译文', 'zh-CN@family@gold')
        cache.set('source', '新译文', 'zh-CN@family@gnew')
        self.assertEqual(cache.get_latest_compatible('source', 'zh-CN@family'), '新译文')
        with sqlite3.connect(cache.db_path) as conn:
            plan = conn.execute('EXPLAIN QUERY PLAN SELECT translated_html FROM translations WHERE source_html=? AND (target_lang=? OR target_lang LIKE ?) ORDER BY rowid DESC LIMIT 1',
                                ('source', 'zh-CN@family', 'zh-CN@family@%')).fetchall()
        self.assertIn('translations_source_idx', str(plan))

    def test_glossary_requests_overlap_and_are_bounded(self):
        active = peak = 0
        async def request(**kwargs):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            await asyncio.sleep(.015)
            items = json.loads(kwargs['messages'][1]['content'].split('候选术语：', 1)[1])
            active -= 1
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
                {'translations': {item['term']: '统一译名' for item in items}})))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        stats = {}
        candidates = [GlossaryCandidate(f'Trader {n}', 2) for n in range(6)]
        with patch('openai.AsyncOpenAI', return_value=client):
            result = asyncio.run(translate_glossary(candidates, max_terms_per_call=1, metrics=stats))
        self.assertEqual(peak, 2)
        self.assertEqual(len(result), 6)
        self.assertEqual(stats['llm_batches_successful'], 6)
        self.assertEqual(stats['llm_api_calls'], 6)
        self.assertEqual(stats['llm_batches_failed'], 0)

    def test_parallel_glossary_balance_failure_stops_queued_batches(self):
        class BalanceError(RuntimeError): status_code = 402
        request = AsyncMock(side_effect=BalanceError('Insufficient Balance'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        with patch('openai.AsyncOpenAI', return_value=client):
            with self.assertRaises(ProviderAccountUnavailable):
                asyncio.run(translate_glossary([GlossaryCandidate(f'Trader {n}', 2) for n in range(8)], max_terms_per_call=1))
        self.assertLessEqual(request.await_count, 2)  # Already in-flight requests cannot be unsent.
        self.assertGreater(request.await_count, 0)

    def test_glossary_soft_time_limit_is_not_swallowed(self):
        request = AsyncMock(side_effect=SoftTimeLimitExceeded())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        with patch('openai.AsyncOpenAI', return_value=client):
            with self.assertRaises(SoftTimeLimitExceeded):
                asyncio.run(translate_glossary([GlossaryCandidate('Trader', 2)]))

    def test_missing_sdk_fallback_is_not_a_complete_glossary(self):
        stats = {}
        with patch.dict('sys.modules', {'openai': None}):
            result = asyncio.run(translate_glossary([GlossaryCandidate('Trader', 2)], metrics=stats))
        self.assertEqual(result, {})
        self.assertEqual(stats['llm_batches_failed'], 1)

    def test_disallowed_model_fallback_is_not_a_complete_glossary(self):
        stats = {}
        with patch('app.infra.llm_guard.assert_model_allowed', side_effect=ModelNotAllowedError('disallowed')):
            result = asyncio.run(translate_glossary([GlossaryCandidate('Trader', 2)], metrics=stats))
        self.assertEqual(result, {})
        self.assertEqual(stats['llm_batches_failed'], 1)

    def test_absolute_deadline_cancels_underlying_operation(self):
        closed = []
        async def operation():
            try: await asyncio.Event().wait()
            finally: closed.append(True)
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(bounded_request(operation(), timeout=.02))
        self.assertEqual(closed, [True])

    def test_cancellation_checked_before_accepting_late_response(self):
        cancelled = [False]
        async def operation():
            cancelled[0] = True
            return 'late'
        with self.assertRaises(JobCancelled):
            asyncio.run(bounded_request(operation(), timeout=1, cancel_check=lambda: cancelled[0]))

    def test_request_failure_cancels_sibling_before_returning(self):
        async def run():
            started, stopped = asyncio.Event(), asyncio.Event()
            async def waiting():
                started.set()
                try: await asyncio.Event().wait()
                finally: stopped.set()
            async def failure():
                await started.wait()
                raise ProviderAccountUnavailable('primary')
            with self.assertRaises(ProviderAccountUnavailable):
                await gather_cancel_on_error(waiting(), failure())
            self.assertTrue(stopped.is_set())
        asyncio.run(run())

    def test_inflight_sdk_request_is_reserved_and_resume_keeps_budget(self):
        manifest = manifest_for(ALPHA)
        response = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({'results': [{'id': 0, 'translation': TRANSLATIONS[ALPHA]}]})))])
        request = AsyncMock(side_effect=SoftTimeLimitExceeded())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        with patch.dict(os.environ, {'EPUB_TRANSLATION_CHUNK_RETRY_BUDGET': '2', 'EPUB_FAILED_CHUNK_RESCUE': '0'}), \
             patch.object(SemanticsTranslator, '_get_client', return_value=client):
            with self.assertRaises(SoftTimeLimitExceeded): self.run_manifest(manifest)
            with sqlite3.connect(self.db) as conn:
                saved = json.loads(conn.execute("SELECT payload FROM book_translation_checkpoints WHERE item_key='chunk:c0_0'").fetchone()[0])
            self.assertEqual(saved['result']['retry_count'], 1)
            self.assertEqual(saved['result']['error'], 'inflight request interrupted')
            request.reset_mock(); request.side_effect = None; request.return_value = response
            stats, _ = self.run_manifest(manifest)
            self.assertEqual(request.await_count, 1)
            self.assertEqual(stats['failed_chunks'], 0)
            with sqlite3.connect(self.db) as conn:
                saved = json.loads(conn.execute("SELECT payload FROM book_translation_checkpoints WHERE item_key='chunk:c0_0'").fetchone()[0])
            self.assertEqual(saved['result']['retry_count'], 2)
            self.assertEqual(saved['phase'], 'final')
            request.reset_mock()
            stats, _ = self.run_manifest(manifest)
            self.assertEqual(request.await_count, 0)
            with sqlite3.connect(self.db) as conn:
                saved = json.loads(conn.execute("SELECT payload FROM book_translation_checkpoints WHERE item_key='chunk:c0_0'").fetchone()[0])
            self.assertEqual(saved['result']['retry_count'], 2)

    def test_early_rescue_inflight_request_also_survives_worker_pause(self):
        untranslated = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({'results': [{'id': 0, 'translation': ALPHA}]})))])
        request = AsyncMock(side_effect=[untranslated, untranslated, SoftTimeLimitExceeded()])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        with patch.dict(os.environ, {'EPUB_TRANSLATION_CHUNK_RETRY_BUDGET': '2', 'EPUB_FAILED_CHUNK_RESCUE': '1'}), \
             patch.object(SemanticsTranslator, '_get_client', return_value=client):
            with self.assertRaises(SoftTimeLimitExceeded): self.run_manifest(manifest_for(ALPHA))
            self.assertEqual(request.await_count, 3)
            with sqlite3.connect(self.db) as conn:
                saved = json.loads(conn.execute("SELECT payload FROM book_translation_checkpoints WHERE item_key='chunk:c0_0'").fetchone()[0])
            self.assertEqual(saved['result']['retry_count'], 2)
            request.reset_mock()
            stats, _ = self.run_manifest(manifest_for(ALPHA))
        self.assertEqual(request.await_count, 0)
        self.assertEqual(stats['failed_chunks'], 1)

    def test_inflight_interruption_cannot_reset_exhausted_sdk_budget(self):
        request = AsyncMock(side_effect=SoftTimeLimitExceeded())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        with patch.dict(os.environ, {'EPUB_TRANSLATION_CHUNK_RETRY_BUDGET': '1', 'EPUB_FAILED_CHUNK_RESCUE': '0'}), \
             patch.object(SemanticsTranslator, '_get_client', return_value=client):
            with self.assertRaises(SoftTimeLimitExceeded): self.run_manifest(manifest_for(ALPHA))
            request.reset_mock()
            stats, _ = self.run_manifest(manifest_for(ALPHA))
        self.assertEqual(request.await_count, 0)
        self.assertEqual(stats['failed_chunks'], 1)

    def test_multi_request_rescue_reserves_cumulative_not_per_call_count(self):
        t = SemanticsTranslator(target_lang='zh-CN')
        response = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({'results': [{'id': 0, 'translation': TRANSLATIONS[ALPHA]}]})))])
        request = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        observed = []
        async def two_calls():
            await t._call_llm_json_batch([{'id': 0, 'html': ALPHA}])
            return await t._call_llm_json_batch([{'id': 0, 'html': ALPHA}])
        with patch.object(t, '_get_client', return_value=client):
            asyncio.run(_within_request_budget(2, two_calls, on_request=observed.append))
        self.assertEqual(observed, [1, 2])
        self.assertEqual(request.await_count, 2)

    def test_successful_chapter_survives_pause_and_new_store_attempt(self):
        manifest = manifest_for(ALPHA, BETA)
        async def paused(translator, payload, **kwargs):
            if BETA in payload[0]['html']:
                await asyncio.sleep(.025)
                raise ProviderAccountUnavailable('primary')
            return reply({item['id']: TRANSLATIONS[ALPHA] for item in payload})
        with patch.object(SemanticsTranslator, '_call_llm_json_batch', new=paused):
            with self.assertRaises(ProviderAccountUnavailable): self.run_manifest(manifest)
        # Simulate a new worker/store, with progress rows removed by user retry.
        self.store.clear_translation_progress(self.job.id)
        self.job.translation_stats = {'attempt_id': 'attempt-2', 'translation_attempt': 2}
        seen = []
        async def recover(translator, payload, **kwargs):
            seen.extend(item['html'] for item in payload)
            return reply({item['id']: TRANSLATIONS[BETA] for item in payload})
        with patch.object(SemanticsTranslator, '_call_llm_json_batch', new=recover):
            stats, _ = self.run_manifest(manifest)
        self.assertEqual(stats['checkpoint_resumed_chunks'], 1)
        self.assertEqual(stats['failed_chunks'], 0)
        self.assertTrue(all(BETA in source for source in seen))
        self.assertEqual(stats['cached_chunks'], 1)

    def test_healthy_sibling_is_saved_before_bad_sibling_rescue_pauses(self):
        t = SemanticsTranslator(target_lang='zh-CN')
        saved = {}
        async def call(payload, **kwargs):
            if len(payload) > 1:
                return reply({0: ALPHA, 1: TRANSLATIONS[BETA]})
            raise ProviderAccountUnavailable('primary')
        t._call_llm_json_batch = call
        with self.assertRaises(ProviderAccountUnavailable):
            asyncio.run(t.translate_many_chunks_async([f'<p>{ALPHA}</p>', f'<p>{BETA}</p>'],
                result_callback=lambda index, result: saved.update({index: result})))
        self.assertIn(1, saved)
        self.assertEqual(saved[1].translated_html, TRANSLATIONS[BETA])

    def test_partially_finished_chapter_resumes_draft_after_worker_pause(self):
        manifest = manifest_for(ALPHA)
        manifest['chapters'][0]['chunks'].append({'chunk_id': 'c0_1', 'sequence': 1,
            'locator': '/html[1]/body[1]/p[2]', 'html': f'<p>{BETA}</p>', 'text': BETA,
            'translation_strategy': 'html'})
        async def paused(translator, payload, **kwargs):
            if len(payload) == 2: return reply({0: TRANSLATIONS[ALPHA], 1: BETA})
            raise ProviderAccountUnavailable('primary')
        with patch.object(SemanticsTranslator, '_call_llm_json_batch', new=paused):
            with self.assertRaises(ProviderAccountUnavailable): self.run_manifest(manifest)
        self.job.translation_stats = {'attempt_id': 'attempt-2', 'translation_attempt': 2}
        async def recover(translator, payload, **kwargs):
            self.assertTrue(all(BETA in item['html'] for item in payload))
            return reply({item['id']: TRANSLATIONS[BETA] for item in payload})
        with patch.object(SemanticsTranslator, '_call_llm_json_batch', autospec=True, side_effect=recover) as request:
            stats, _ = self.run_manifest(manifest)
        self.assertEqual(request.await_count, 1)
        self.assertEqual(stats['checkpoint_resumed_chunks'], 1)
        self.assertEqual(stats['failed_chunks'], 0)

    def test_corrupt_or_english_checkpoint_is_rejected_by_current_qa(self):
        manifest = manifest_for(ALPHA)
        async def call(translator, payload, **kwargs): return reply({item['id']: TRANSLATIONS[ALPHA] for item in payload})
        with patch.object(SemanticsTranslator, '_call_llm_json_batch', new=call): self.run_manifest(manifest)
        with sqlite3.connect(self.db) as conn:
            scope, raw = conn.execute("SELECT scope,payload FROM book_translation_checkpoints WHERE item_key='chunk:c0_0'").fetchone()
            payload = json.loads(raw); payload['result']['translated_html'] = ALPHA
            conn.execute("UPDATE book_translation_checkpoints SET payload=? WHERE scope=? AND item_key='chunk:c0_0'", (json.dumps(payload), scope))
        with patch.object(SemanticsTranslator, '_call_llm_json_batch', autospec=True, side_effect=call) as request:
            stats, _ = self.run_manifest(manifest)
        self.assertEqual(stats['checkpoint_rejected_chunks'], 1)
        self.assertEqual(stats['checkpoint_resumed_chunks'], 0)
        self.assertEqual(request.await_count, 1)

    def test_completed_high_quality_checkpoint_does_not_repeat_review(self):
        self.job.translation_quality = 'high'
        manifest = manifest_for(ALPHA)
        async def call(translator, payload, **kwargs): return reply({item['id']: TRANSLATIONS[ALPHA] for item in payload})
        async def review(translator, sources, drafts, **kwargs): return drafts
        with patch.object(SemanticsTranslator, '_call_llm_json_batch', new=call), \
             patch.object(SemanticsTranslator, 'review_many_chunks_async', autospec=True, side_effect=review) as checker:
            self.run_manifest(manifest)
            self.assertEqual(checker.await_count, 1)
            checker.reset_mock()
            stats, _ = self.run_manifest(manifest)
            self.assertEqual(checker.await_count, 0)
        self.assertEqual(stats['checkpoint_resumed_chunks'], 1)

    def test_exhausted_budget_survives_same_attempt_worker_restart(self):
        manifest = manifest_for(ALPHA)
        async def call(translator, payload, **kwargs): return reply({item['id']: item['html'] for item in payload})
        with patch.dict(os.environ, {'EPUB_TRANSLATION_CHUNK_RETRY_BUDGET': '1', 'EPUB_FAILED_CHUNK_RESCUE': '0'}), \
             patch.object(SemanticsTranslator, '_call_llm_json_batch', autospec=True, side_effect=call) as request:
            stats, _ = self.run_manifest(manifest)
            self.assertEqual(stats['failed_chunks'], 1)
            request.reset_mock()
            stats, _ = self.run_manifest(manifest)
            self.assertEqual(request.await_count, 0)
            self.assertEqual(stats['failed_chunks'], 1)

    def test_rescue_overlaps_other_chapter_and_releases_chapter_slot(self):
        manifest = manifest_for(ALPHA, BETA)
        counts = {ALPHA: 0}
        async def run():
            beta_started = asyncio.Event()
            async def call(translator, payload, **kwargs):
                source = payload[0]['html']
                if ALPHA in source:
                    counts[ALPHA] += 1
                    if counts[ALPHA] <= 2: return reply({0: source})
                    await asyncio.wait_for(beta_started.wait(), .5)
                    return reply({0: TRANSLATIONS[ALPHA]})
                beta_started.set()
                await asyncio.sleep(.025)
                return reply({0: TRANSLATIONS[BETA]})
            with patch.dict(os.environ, {'EPUB_CHAPTER_CONCURRENCY_CAP': '1'}), \
                 patch.object(SemanticsTranslator, '_call_llm_json_batch', new=call):
                return await asyncio.wait_for(_translate_manifest_async(job=self.job, manifest=manifest,
                    content_by_file={}, glossary={}, progress_callback=lambda _: None), 2)
        stats, _ = asyncio.run(run())
        self.assertEqual(stats['failed_chunks'], 0)
        self.assertEqual(stats['failed_chunk_rescue_succeeded'], 1)

    def test_rescue_limit_is_shared_across_chapters(self):
        texts = [ALPHA, BETA, 'Gamma proposes another approach.']
        counts = {text: 0 for text in texts}
        active = peak = 0
        async def call(translator, payload, **kwargs):
            nonlocal active, peak
            text = payload[0]['html']
            counts[text] += 1
            if counts[text] <= 2: return reply({0: text})
            active += 1; peak = max(peak, active)
            try:
                await asyncio.sleep(.03)
                return reply({0: '交易者提出这种方法。'})
            finally: active -= 1
        with patch.dict(os.environ, {'EPUB_CHAPTER_CONCURRENCY_CAP': '3',
                                    'EPUB_FAILED_CHUNK_RESCUE_CONCURRENCY': '2',
                                    'EPUB_FAILED_CHUNK_RESCUE_CONCURRENCY_CAP': '2'}), \
             patch.object(SemanticsTranslator, '_call_llm_json_batch', new=call):
            stats, _ = self.run_manifest(manifest_for(*texts))
        self.assertEqual(peak, 2)
        self.assertEqual(stats['failed_chunk_rescue_succeeded'], 3)
        self.assertEqual(stats['failed_chunks'], 0)

    def test_whole_book_retry_reuses_glossary_profile_title_and_final_chunks(self):
        path = Path(self.tmp.name) / 'book.epub'
        book = epub.EpubBook(); book.set_identifier('offline-resume'); book.set_title('A Study'); book.set_language('en')
        chapter = epub.EpubHtml(title='Chapter', file_name='chapter.xhtml', lang='en')
        chapter.content = f'<html><body><p>{ALPHA}</p></body></html>'
        book.add_item(chapter); book.spine = [chapter]
        book.toc = (epub.Link('chapter.xhtml', 'Chapter', 'c'),)
        book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav()); epub.write_epub(str(path), book)
        self.job.input_path = str(path)
        def preprocess(source, output, *args, **kwargs):
            shutil.copyfile(source, output)
            return ConversionResult(validation_passed=True)
        async def call(translator, payload, **kwargs):
            return reply({item['id']: TRANSLATIONS[ALPHA] if ALPHA in item['html'] else '一项研究' for item in payload})
        profile = {'status': 'ok', 'genre': 'nonfiction', 'confidence': .9, 'recommended_strategy': 'neutral_faithful', 'characters': []}
        glossary = GlossaryBuildResult(glossary={}, stats={'auto_glossary_complete': True})
        def run():
            return run_fast_translation_job(job=self.job, input_path=path,
                output_path=Path(self.tmp.name)/'out.epub', progress_callback=lambda _: None,
                stage_callback=lambda *args: None)
        with patch('app.domain.fast_translation_runner.converter.convert_file_to_horizontal', side_effect=preprocess), \
             patch('app.domain.fast_translation_runner._run_epubcheck', return_value=True), \
             patch('app.domain.fast_translation_runner.profile_book', return_value=profile) as profiler, \
             patch('app.domain.fast_translation_runner.build_consistent_glossary', return_value=glossary) as terminology, \
             patch.object(SemanticsTranslator, '_call_llm_json_batch', autospec=True, side_effect=call) as request:
            run(); self.assertGreater(request.await_count, 0)
            request.reset_mock()
            self.job.translation_stats = {'attempt_id': 'attempt-2', 'translation_attempt': 2}
            result = run()
            self.assertEqual(request.await_count, 0)
            self.assertEqual(profiler.call_count, 1)
            self.assertEqual(terminology.call_count, 1)
        self.assertTrue(all(result.translation_stats['preparation_checkpoint_hits'].values()))
        self.assertEqual(result.translation_stats['checkpoint_resumed_chunks'], 1)
        self.assertIn('Total', result.translation_stats['phase_timings_ms'])

    @unittest.skipUnless(os.environ.get('EPUB_REGRESSION_BOOK'), 'selected real source book not provided')
    def test_real_book_resume_fingerprint_is_stable_and_catches_image_bullet_change(self):
        manifest = build_manifest(os.environ['EPUB_REGRESSION_BOOK'], self.job.id)
        self.assert_real_manifest_scope(manifest)
        with zipfile.ZipFile(os.environ['EPUB_REGRESSION_BOOK']) as archive:
            navigation = audit_epub_navigation(archive, sample_limit=0)
        self.assertGreater(navigation['navigation_labels_checked'], 0)
        self.assertEqual(navigation['navigation_broken_targets'], 0)
        key = book_resume_key(self.job, manifest)
        self.job.translation_stats['attempt_id'] = 'new-real-book-attempt'
        self.assertEqual(key, book_resume_key(self.job, manifest))
        changed = copy.deepcopy(manifest)
        bullet = next(c for ch in changed['chapters'] for c in ch['chunks'] if '<img' in c['html'] and c['char_count']>100)
        bullet['html'] = bullet['html'].replace('<img', '<img data-changed="yes"', 1)
        self.assertNotEqual(key, book_resume_key(self.job, changed))

    def assert_real_manifest_scope(self, manifest):
        # The previous 2681 count included this secondary HTML contents page.
        # Keep its 19 blocks accounted for separately; changing the body total
        # alone could conceal a newly omitted document or navigation content.
        self.assertEqual(sum(len(ch['chunks']) for ch in manifest['chapters'] if ch['chapter_kind']=='body'), 2662)
        contents = next(ch for ch in manifest['chapters'] if ch['file_path'] == 'cS.xhtml')
        self.assertEqual(contents['chapter_kind'], 'nav')
        self.assertEqual(len(contents['chunks']), 19)
        blocks = [BeautifulSoup(spec['html'], 'html.parser') for spec in contents['chunks']]
        self.assertEqual(sum(bool(block.find(['h1', 'h2', 'h3'])) for block in blocks), 1)
        self.assertEqual(sum(bool(block.find('a', href=True)) for block in blocks), 18)
        return blocks

    @unittest.skipUnless(os.environ.get('EPUB_REGRESSION_BOOK') and os.environ.get('EPUB_REGRESSION_TRANSLATED_BOOK'),
                         'selected real original and formal translated book not provided')
    def test_real_twenty_block_image_bullet_chapter_resumes_without_requests(self):
        source = build_manifest(os.environ['EPUB_REGRESSION_BOOK'], self.job.id)
        target = build_manifest(os.environ['EPUB_REGRESSION_TRANSLATED_BOOK'], self.job.id)
        source_contents = self.assert_real_manifest_scope(source)
        target_contents = self.assert_real_manifest_scope(target)
        self.assertEqual(
            {ch['file_path']: len(ch['chunks']) for ch in source['chapters'] if ch['chapter_kind'] == 'body'},
            {ch['file_path']: len(ch['chunks']) for ch in target['chapters'] if ch['chapter_kind'] == 'body'},
        )
        with zipfile.ZipFile(os.environ['EPUB_REGRESSION_TRANSLATED_BOOK']) as archive:
            # This book's user-approved preserved name is documented in
            # docs/QUALITY-FOLLOWUP-2026-09-17.md. Keep the exception local to
            # this fixture; it must not become a global residual exemption.
            navigation = audit_epub_navigation(archive, preserved_terms=['KELVIN CHIU'], sample_limit=0)
        self.assertGreater(navigation['navigation_labels_checked'], 0)
        self.assertEqual(navigation['navigation_residual_labels'], 0)
        self.assertEqual(navigation['navigation_broken_targets'], 0)
        self.assertEqual(
            [link['href'] for block in source_contents for link in block.find_all('a', href=True)],
            [link['href'] for block in target_contents for link in block.find_all('a', href=True)],
        )
        for index, block in enumerate(target_contents):
            label = block.get_text('', strip=False).strip()
            # This printed contents page decorates the approved name with a
            # page number. Only strip that numeric/punctuation frame when the
            # remaining label exactly matches this fixture's approved name.
            undecorated = re.sub(r'^[\W\d_]+|[\W\d_]+$', '', label)
            if normalize_text(undecorated) == normalize_text('KELVIN CHIU'):
                label = undecorated
            self.assertFalse(residual_category(label, title_like=True,
                                              preserved_terms=['KELVIN CHIU']),
                             f'secondary contents block {index} has untranslated text')
        chapter = next(ch for ch in source['chapters'] if ch['chapter_id'] == 'c38')
        translations = {spec['chunk_id']: spec for ch in target['chapters'] for spec in ch['chunks']}
        lookup = {}
        for spec in chapter['chunks']:
            src = SemanticsTranslator._extract_inner_html(spec['html'])
            dst = SemanticsTranslator._extract_inner_html(translations[spec['chunk_id']]['html'])
            lookup[src] = dst
            left = [node for node in BeautifulSoup(src, 'html.parser').find_all(string=True) if is_external_text_node(node)]
            right = [node for node in BeautifulSoup(dst, 'html.parser').find_all(string=True) if is_external_text_node(node)]
            self.assertEqual(len(left), len(right))
            lookup.update({str(a): str(b) for a, b in zip(left, right)})
        async def reviewed_response(translator, payload, **kwargs):
            # Replay the reviewed real output as controlled model responses;
            # does not call DeepSeek or fabricate a fresh customer translation.
            return reply({item['id']: lookup[item['html']] for item in payload})
        manifest = {'chapters': [chapter]}
        with zipfile.ZipFile(os.environ['EPUB_REGRESSION_BOOK']) as archive:
            document = next(name for name in archive.namelist()
                            if name == chapter['file_path'] or name.endswith('/' + chapter['file_path']))
            original_document = archive.read(document)
        def structure(document):
            return [(tag.name, tag.attrs) for tag in BeautifulSoup(document, 'html.parser').find_all(True)]
        first_content = {chapter['file_path']: original_document}
        resumed_content = {chapter['file_path']: original_document}
        written_documents = []
        with patch.dict(os.environ, {'EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE': '1'}), \
             patch.object(SemanticsTranslator, '_call_llm_json_batch', autospec=True, side_effect=reviewed_response) as request, \
             patch('app.domain.fast_translation_runner.set_chapter_output',
                   side_effect=lambda job_id, file_path, content: written_documents.append(content)):
            first, _ = self.run_manifest(manifest, first_content)
            self.assertGreater(request.await_count, 0)
            self.assertEqual(first['failed_chunks'], 0)
            request.reset_mock()
            self.job.translation_stats = {'attempt_id': 'real-book-retry', 'translation_attempt': 2}
            resumed, _ = self.run_manifest(manifest, resumed_content)
            self.assertEqual(request.await_count, 0)
        self.assertEqual(len(written_documents), 2)
        self.assertTrue(original_document != written_documents[0])
        self.assertEqual(structure(original_document), structure(written_documents[0]))
        self.assertEqual(structure(original_document), structure(written_documents[1]))
        self.assertTrue(written_documents[0] == written_documents[1])
        self.assertEqual(len(chapter['chunks']), 20)
        self.assertEqual(sum('<img' in spec['html'] for spec in chapter['chunks']), 9)
        self.assertEqual(resumed['checkpoint_resumed_chunks'], 20)
        self.assertEqual(resumed['failed_chunks'], 0)


if __name__ == '__main__': unittest.main(verbosity=2)
