"""Offline durable pre-payment budget/cache tests; never contacts a model."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

with patch('dotenv.load_dotenv', return_value=False):
    from app.domain.preflight_admission import (
        PreflightAdmission, PreflightAdmissionError, PreflightBudget, PreflightCache,
        admitted_preflight, fingerprint,
    )
    from app.domain import translation_preflight_service as service
    from app.infra.llm_gateway import governed_request, dispatch_budget_scope
    from app.infra.llm_usage_ledger import get_ledger, usage_scope


class PreflightAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='preflight-budget-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = create_engine('sqlite:///' + str(self.root / 'budget.db'),
                                    connect_args={'timeout': 20})
        self.addCleanup(self.engine.dispose)
        self.env = patch.dict(os.environ, {
            'EPUB_LLM_RATE_LIMITER_ENABLED': '0', 'EPUB_LLM_GLOBAL_HEALTH_ENABLED': '0',
            'EPUB_PREFLIGHT_BOOK_REQUESTS': '2', 'EPUB_PREFLIGHT_BOOK_UNITS': '200000',
            'EPUB_PREFLIGHT_DAILY_REQUESTS': '3', 'EPUB_PREFLIGHT_DAILY_UNITS': '1000000',
            'EPUB_PREFLIGHT_CACHE_SECONDS': '3600',
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.messages = [{'role': 'user', 'content': 'Controlled input'}]
        self.source = self.root / 'source.epub'
        self.source.write_bytes(b'only a fingerprint fixture; never parsed')

    def admission(self, *, source='book-sha', config='config', principal='session', subjects=None):
        return PreflightAdmission(engine=self.engine, source_sha256=source, config_hash=config,
                                  principal=principal, budget_subjects=subjects or ['ip:127.0.0.1'])

    def reserve(self, admission):
        admission.reserve(100, messages=self.messages, max_output_tokens=4096)

    def counts(self):
        with Session(self.engine) as session:
            return {row.key: (row.requests, row.units) for row in session.scalars(select(PreflightBudget))}

    def test_cached_result_is_private_and_survives_new_admission(self):
        first = self.admission()
        first.claim()
        self.reserve(first)
        first.finish({'translation': 'safe result'})
        second = self.admission()
        self.assertEqual(second.claim(), {'translation': 'safe result'})
        self.assertIsNone(self.admission(principal='different-user').claim())
        self.assertTrue(all(count[0] == 1 for count in self.counts().values()))

    def test_concurrent_claim_allows_only_one_owner(self):
        admissions = [self.admission() for _ in range(10)]
        def claim(item):
            try:
                item.claim()
                return 'owner'
            except PreflightAdmissionError as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(claim, admissions))
        self.assertEqual(results.count('owner'), 1)
        self.assertEqual(results.count(409), 9)

    def test_running_expired_claim_is_not_taken_over(self):
        first = self.admission()
        first.claim()
        with Session(self.engine) as session:
            session.get(PreflightCache, first.cache_key).expires_at = 0
            session.commit()
        with self.assertRaises(PreflightAdmissionError) as failure:
            self.admission().claim()
        self.assertEqual(failure.exception.status_code, 409)

    def test_expired_success_can_rebuild_but_budget_does_not_reset(self):
        first = self.admission()
        first.claim()
        self.reserve(first)
        first.finish({'ok': True})
        with Session(self.engine) as session:
            session.get(PreflightCache, first.cache_key).expires_at = 0
            session.commit()
        second = self.admission()
        self.assertIsNone(second.claim())
        self.reserve(second)
        with self.assertRaises(PreflightAdmissionError):
            self.reserve(second)

    def test_book_budget_survives_session_and_model_change(self):
        first = self.admission()
        first.claim()
        self.reserve(first)
        self.reserve(first)
        first.finish()
        other = self.admission(principal='rotated-session', config='other-model')
        other.claim()
        with self.assertRaises(PreflightAdmissionError):
            self.reserve(other)
        self.assertTrue(all(count[0] == 2 for count in self.counts().values()))

    def test_daily_budget_shared_across_books_and_atomic_subjects(self):
        for index in range(3):
            item = self.admission(source=str(index), config=str(index))
            item.claim()
            self.reserve(item)
            item.finish()
        other = self.admission(source='fourth', config='fourth', subjects=['user:1', 'ip:127.0.0.1'])
        other.claim()
        before = self.counts()
        with self.assertRaises(PreflightAdmissionError):
            self.reserve(other)
        self.assertEqual(self.counts(), before)

    def test_concurrent_reservations_never_overspend(self):
        item = self.admission()
        item.claim()
        def reserve(_):
            try:
                self.reserve(item)
                return True
            except PreflightAdmissionError:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            admitted = list(pool.map(reserve, range(12)))
        self.assertEqual(sum(admitted), 2)
        self.assertTrue(all(count[0] == 2 for count in self.counts().values()))

    def test_failure_retains_reservation_and_releases_cache_owner(self):
        with self.assertRaises(RuntimeError):
            with admitted_preflight(engine=self.engine, source_sha256='book-sha', config_hash='config',
                                    principal='session', budget_subjects=['ip:127.0.0.1']) as item:
                self.reserve(item)
                raise RuntimeError('controlled transport failure')
        self.assertIsNone(self.admission().claim())
        self.assertTrue(all(count[0] == 1 for count in self.counts().values()))

    def test_unit_limit_and_missing_output_cap_reject_before_dispatch(self):
        with patch.dict(os.environ, {'EPUB_PREFLIGHT_BOOK_UNITS': '100'}):
            item = self.admission()
        item.claim()
        for cap in (None, 4096):
            factory = AsyncMock()
            with usage_scope('denied', 'preflight', engine=self.engine), dispatch_budget_scope(item.reserve):
                with self.assertRaises(PreflightAdmissionError):
                    asyncio.run(governed_request(factory, model='deepseek-flash',
                        base_url='https://api.deepseek.com/v1', messages=self.messages, max_output_tokens=cap))
            factory.assert_not_called()
        self.assertEqual(get_ledger(self.engine).requests('denied'), [])
        self.assertEqual(self.counts(), {})

    def test_invalid_configuration_fails_closed(self):
        for value in ('0', '-1', 'invalid', '1001'):
            with patch.dict(os.environ, {'EPUB_PREFLIGHT_BOOK_REQUESTS': value}):
                with self.assertRaises(PreflightAdmissionError):
                    self.admission()

    def test_fingerprint_detects_inputs_and_config_without_storing_secrets(self):
        before = fingerprint(self.source, options={'model': 'a'})
        self.assertEqual(before, fingerprint(self.source, options={'model': 'a'}))
        self.assertNotEqual(before, fingerprint(self.source, options={'model': 'b'}))
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-private-key'}):
            after = fingerprint(self.source, options={'model': 'a'})
        self.assertNotEqual(before, after)
        self.assertNotIn('test-private-key', repr(after))

    def test_normalized_metadata_change_invalidates_cache_not_book_budget(self):
        first = fingerprint(self.source, source_sha256='same-original')
        self.source.write_bytes(b'different normalized title with identical original bytes')
        second = fingerprint(self.source, source_sha256='same-original')
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])

    def test_build_preflight_duplicate_does_not_repeat_model_or_ledger(self):
        kwargs = dict(epub_path=self.source, target_lang='zh', translation_model='deepseek-flash',
                      requested_strategy='auto', billing_engine=self.engine, budget_principal='u1',
                      budget_subjects=['ip:127.0.0.1'])
        with patch.object(service, '_build_translation_preflight', return_value={'profile': {}, 'glossary': []}) as build:
            first = service.build_translation_preflight(job_id='job-1', **kwargs)
            second = service.build_translation_preflight(job_id='job-2', **kwargs)
        self.assertEqual(build.call_count, 1)
        self.assertFalse(first['preflight_cache']['hit'])
        self.assertEqual(second['preflight_cache'], {'hit': True, 'new_model_requests': 0})
        self.assertEqual(get_ledger(self.engine).requests('job-2'), [])

    def test_job_boundary_marks_governor_abort_failed_without_retry(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from app.job_runner import run_job
        from app.models import Job, JobStatus, OutputMode, ErrorCode
        from app.storage import JobStore
        from app.infra.llm_gateway import GatewayControlError
        store = JobStore()
        store._engine = self.engine
        job = Job(id='governor-job', source_filename='source.epub', trace_id='offline',
                  input_path=str(self.source), output_mode=OutputMode.simplified,
                  enable_translation=True, status=JobStatus.pending,
                  translation_stats={'attempt_id': 'one', 'translation_attempt': 1})
        store.add(job)
        lease = nullcontext(SimpleNamespace(assert_owned=lambda: None, owner='offline'))
        with patch('app.job_runner.job_store', store), patch('app.job_runner.OUTPUT_DIR', self.root), \
             patch('app.job_runner.execution_lease', return_value=lease), \
             patch('app.domain.fast_translation_runner.run_fast_translation_job',
                   side_effect=GatewayControlError('controlled quota unavailable')) as execute, \
             patch('app.job_runner.report_error'), patch('app.job_runner.notify_job_completed'), \
             patch('app.job_runner.logger.exception'):
            run_job(job.id, 'one')
        failed = store.get(job.id)
        self.assertEqual(failed.status, JobStatus.failed)
        self.assertEqual(failed.error_code, ErrorCode.TRANSLATION_FAILED)
        self.assertTrue(failed.translation_stats['model_governor_blocked'])
        self.assertFalse(failed.translation_stats['deliverable'])
        self.assertEqual(execute.call_count, 1)

    def test_upload_budget_denial_is_public_and_creates_no_order(self):
        from fastapi.testclient import TestClient
        from test_epub_fixture import minimal_epub_bytes
        import app.main as main
        uploads = self.root / 'uploads'
        uploads.mkdir()
        for status in (409, 429, 503):
            with patch.object(main, 'UPLOAD_DIR', uploads), \
                 patch.object(main, '_prepare_translation_request',
                              side_effect=PreflightAdmissionError('controlled admission denial', status_code=status)) as prepare, \
                 patch.object(main.job_store, 'add') as add, \
                 patch.object(main, 'create_alipay_page_pay') as pay, \
                 patch.object(main, '_enqueue_conversion') as enqueue:
                response = TestClient(main.app).post('/api/v2/jobs',
                    files={'file': ('book.epub', minimal_epub_bytes(), 'application/epub+zip')},
                    data={'enable_translation': 'true', 'profile_confirmation': 'true'},
                    headers={'X-Client-Session': 'offline-budget-client', 'X-Forwarded-For': 'forged-ip'})
            self.assertEqual(response.status_code, status, response.text)
            self.assertIn('controlled admission denial', response.text)
            self.assertEqual(list(uploads.iterdir()), [])
            add.assert_not_called()
            pay.assert_not_called()
            enqueue.assert_not_called()
            self.assertNotIn('forged-ip', repr(prepare.call_args.kwargs['budget_subjects']))


if __name__ == '__main__':
    unittest.main()
