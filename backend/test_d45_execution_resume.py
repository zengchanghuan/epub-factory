"""R7 same-attempt lost-worker resume through real checkpoints and usage ledger.

Synthetic two-chapter source, not a historical-book quality claim. The real
translator/QA/reduce/checkpoint chain runs; only the SDK transport is controlled.
"""
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import create_engine, update


class ExecutionResumeTests(unittest.TestCase):
    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epub-r7-resume-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._patch(patch.dict(os.environ, {
            'EPUB_PERSISTENT_STORE': '0', 'DATABASE_URL': 'sqlite:///' + str(self.root / 'bootstrap.db'),
            'CELERY_BROKER_URL': '', 'REDIS_URL': '', 'SENTRY_DSN': '',
            'OPENAI_BASE_URL': 'https://api.deepseek.com/v1',
            'EPUB_CHAPTER_CONCURRENCY_CAP': '1', 'EPUB_FAILED_CHUNK_RESCUE': '0',
            'EPUB_FAILED_CHUNK_DIR': str(self.root / 'failed'),
            'NOTIFY_EMAIL_ENABLED': '0', 'OWNER_PAYMENT_EMAIL_ENABLED': '0',
        }, clear=True))
        self._patch(patch('dotenv.load_dotenv', return_value=False))
        self.network = [self._patch(patch(name, side_effect=AssertionError('No R7 network/model calls')))
                        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo')]
        # Import only after the network/config isolation. Reuse setup/helper
        # methods by composition; none of the D29 test methods are inherited.
        import test_d29_translation_performance as previous
        previous.PerformanceTests.setUp(self)
        self.previous = previous
        from app.domain import book_reduce_service, fast_translation_runner
        from app.infra.execution_lease import execution_lease
        from app.infra.llm_usage_ledger import get_ledger, usage_scope
        from app.storage_db import Base, JobRecord, PersistentJobStore
        self.reducer, self.fast = book_reduce_service, fast_translation_runner
        self.lease, self.usage_scope = execution_lease, usage_scope
        self.Store, self.Status, self.JobRecord = PersistentJobStore, previous.JobStatus, JobRecord
        self.engine = create_engine('sqlite:///' + str(self.root / 'jobs.db'),
                                    connect_args={'check_same_thread': False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.job.status = self.Status.pending
        self.store.add(self.job)
        self._patch(patch.object(fast_translation_runner, 'job_store', self.store))
        self._patch(patch.object(book_reduce_service, '_REDUCE_WORK_DIR', self.root / 'reduce'))
        self._patch(patch('app.infra.execution_lease.tempfile.gettempdir', return_value=str(self.root)))
        self.ledger = get_ledger(self.engine)
        self.manifest = previous.manifest_for(previous.ALPHA, previous.BETA)
        self.calls = []
        self.interrupt_beta = True

        async def request(**options):
            user_content = options['messages'][-1]['content']
            # Require an exact fixture source; unexpected pre-analysis/requests
            # are failures, never generic responses that hide extra spending.
            text = previous.ALPHA if previous.ALPHA in user_content else previous.BETA if previous.BETA in user_content else None
            self.assertIsNotNone(text, 'Unexpected model payload')
            self.calls.append(text)
            if text == previous.BETA and self.interrupt_beta:
                raise SoftTimeLimitExceeded()
            return SimpleNamespace(
                id=f'offline-response-{len(self.calls)}', model='deepseek-flash',
                usage=dict(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                           prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60),
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    'results': [{'id': 0, 'translation': previous.TRANSLATIONS[text]}]})))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        self._patch(patch.object(previous.SemanticsTranslator, '_get_client', return_value=client))

    def tearDown(self):
        for network in self.network:
            network.assert_not_called()

    def run_manifest(self):
        with self.usage_scope(self.job.id, 'attempt-1', engine=self.engine,
                              existing_stats=self.job.translation_stats):
            return self.previous.PerformanceTests.run_manifest(self, self.manifest)

    def checkpoints(self):
        with sqlite3.connect(self.db) as connection:
            return {key: json.loads(payload) for key, payload in connection.execute(
                'SELECT item_key,payload FROM book_translation_checkpoints')}

    def recover_and_begin(self):
        # The old process's external lease has exited. Recovery itself also
        # acquires it, then performs the store's owner/age CAS under that proof.
        with self.lease(self.job.id, 'attempt-1') as recovery_lease:
            self.assertIsNotNone(recovery_lease)
            old = self.store.get_execution(self.job.id, 'attempt-1')
            self.assertEqual(self.store.recover_execution(
                self.job.id, 'attempt-1', old['owner'], stale_before=200,
                now=300, max_recoveries=2), 'recovered')
            queued = self.Store(self.engine).get(self.job.id)
            self.assertEqual(queued.status, self.Status.pending)
            self.assertEqual(queued.translation_stats['attempt_id'], 'attempt-1')
            self.assertEqual(self.store.get_execution(self.job.id, 'attempt-1')['recoveries'], 1)

    def assert_partial_resume(self, policy):
        with self.engine.begin() as connection:
            connection.execute(update(self.JobRecord).where(self.JobRecord.id == self.job.id).values(cache_policy=policy))
        self.job = self.store.get(self.job.id)
        # The scope identity and already-completed checkpoint must survive the
        # same attempt's automatic recovery even under the user's fresh policy.
        key = self.previous.book_resume_key(self.job, self.manifest)
        with self.lease(self.job.id, 'attempt-1') as first:
            self.assertTrue(self.store.begin_execution(self.job.id, 'attempt-1', first.owner, now=100))
            with self.assertRaises(SoftTimeLimitExceeded):
                self.run_manifest()
        self.assertEqual(self.calls, [self.previous.ALPHA, self.previous.BETA])
        saved = self.checkpoints()
        self.assertFalse(saved['chunk:c0_0']['result']['error'])
        self.assertIn(self.previous.TRANSLATIONS[self.previous.ALPHA], saved['chunk:c0_0']['result']['translated_html'])
        self.assertEqual(saved['chunk:c1_0']['result']['retry_count'], 1)
        ledger_before = self.ledger.requests(self.job.id)
        self.assertEqual(len(ledger_before), 2)
        completed_before = [row for row in ledger_before if row['request_status'] == 'response']
        self.assertEqual(len(completed_before), 1)
        stats_before = copy.deepcopy(self.store.get(self.job.id).translation_stats)
        self.recover_and_begin()
        self.assertEqual(self.store.get(self.job.id).translation_stats, stats_before)
        self.assertEqual(self.previous.book_resume_key(self.job, self.manifest), key)
        # Reload all frozen options as a new worker would; do not repair the
        # cache policy or manufacture a new attempt in the test.
        self.job = self.Store(self.engine).get(self.job.id)
        self.assertEqual(self.job.cache_policy, policy)
        self.interrupt_beta = False
        with self.lease(self.job.id, 'attempt-1') as second:
            self.assertNotEqual(second.owner, first.owner)
            self.assertTrue(self.store.begin_execution(self.job.id, 'attempt-1', second.owner, now=400))
            stats, _ = self.run_manifest()
            self.assertEqual(stats['checkpoint_resumed_chunks'], 1)
            self.assertEqual(stats['failed_chunks'], 0)
            self.store.update_status(self.job.id, self.Status.success, expected_attempt_id='attempt-1')
            self.assertTrue(self.store.finish_execution(self.job.id, 'attempt-1', second.owner))
        self.assertEqual(self.calls, [self.previous.ALPHA, self.previous.BETA, self.previous.BETA])
        after = self.ledger.requests(self.job.id)
        self.assertEqual(len(after), 3)
        self.assertEqual(after[:2], ledger_before, 'Recovery must not erase/rewrite historical accounting rows')
        self.assertTrue(all(row['attempt_id'] == 'attempt-1' for row in after))
        summary = self.ledger.summary(self.job.id, self.job.translation_stats)
        self.assertEqual(summary['tracked_attempts'], 1)
        self.assertEqual(summary['requests'], 3)
        self.assertEqual(summary['prompt_tokens'], 200)
        self.assertEqual(summary['completion_tokens'], 40)
        self.assertEqual(summary['pending_requests'], 1)  # Interrupted provider usage is unknown, not zero.
        saved_after = self.checkpoints()
        self.assertEqual(saved_after['chunk:c1_0']['result']['retry_count'], 2)
        self.assertEqual(self.store.get_execution(self.job.id, 'attempt-1')['recoveries'], 1)
        for i, text in enumerate((self.previous.ALPHA, self.previous.BETA)):
            output = self.reducer.get_chapter_output(self.job.id, f'c{i}.xhtml', attempt_id='attempt-1')
            self.assertIn(self.previous.TRANSLATIONS[text].encode(), output)

    def test_reuse_policy_worker_recovery_only_requests_remaining_chunk(self):
        self.assert_partial_resume('reuse')

    def test_fresh_policy_same_attempt_worker_recovery_reuses_completed_checkpoint(self):
        self.assert_partial_resume('fresh')

    def test_unfinished_accounting_reservation_is_not_cleared_by_worker_recovery(self):
        with self.lease(self.job.id, 'attempt-1') as first:
            self.assertTrue(self.store.begin_execution(self.job.id, 'attempt-1', first.owner, now=100))
            with self.usage_scope(self.job.id, 'attempt-1', engine=self.engine):
                self.ledger.begin(self.job.id, 'attempt-1', 'body', 'https://api.deepseek.com/v1', 'deepseek-flash')
            # A killed process cannot finish this reservation; do not synthesize
            # a successful/zero-cost receipt during recovery.
        before = self.ledger.requests(self.job.id)
        self.assertEqual(before[0]['request_status'], 'in_flight')
        self.recover_and_begin()
        with self.lease(self.job.id, 'attempt-1') as second:
            self.assertTrue(self.store.begin_execution(self.job.id, 'attempt-1', second.owner, now=400))
            with self.usage_scope(self.job.id, 'attempt-1', engine=self.engine):
                pass
        self.assertEqual(self.ledger.requests(self.job.id), before)
        summary = self.ledger.summary(self.job.id)
        self.assertEqual(summary['requests'], 1)
        self.assertEqual(summary['tracked_attempts'], 1)
        self.assertEqual(summary['pending_reasons'], {'in_flight': 1})
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
