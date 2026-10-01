"""R5 dispatch/restart proof with temporary SQLite and fake broker only."""
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine

with patch('dotenv.load_dotenv', return_value=False):
    from app.domain.job_dispatch_service import dispatch_pending, MAX_BATCH_SIZE
    from app.domain.job_dispatch_worker import JobDispatchWorker
    from app.models import Job, JobStatus, OutputMode
    from app.storage_db import Base, PersistentJobStore


class DispatchServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = create_engine('sqlite:///' + str(self.root / 'dispatch.db'),
                                    connect_args={'check_same_thread': False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.now = time.time() + 3600
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('No external network'))
        network.start(); self.addCleanup(network.stop)

    def job(self, name='job-1', *, attempt='attempt-1', status=JobStatus.pending, translation=True):
        job = Job(id=name, source_filename='private-book.epub', output_mode=OutputMode.simplified,
                  trace_id='trace-' + name, input_path=str(self.root / 'source.epub'), status=status,
                  enable_translation=translation, translation_stats={'attempt_id': attempt} if attempt else {})
        self.store.add(job)
        self.store.ensure_dispatch(name)
        return job

    def record(self, name='job-1'):
        records = self.store.list_dispatches(job_id=name)
        self.assertEqual(len(records), 1)
        return records[0]

    def test_success_preserves_pending_job_and_passes_exact_attempt(self):
        job = self.job()
        publish = Mock()
        result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result, {'claimed': 1, 'published': 1, 'sent': 1, 'obsolete': 0, 'retry': 0, 'errors': 0})
        publish.assert_called_once_with(job.id, 'attempt-1')
        self.assertEqual(self.record()['status'], 'sent')
        self.assertEqual(self.store.get(job.id).status, JobStatus.pending)

    def test_ordinary_empty_and_retry_attempts_are_not_conflated(self):
        self.job('ordinary', attempt='', translation=False)
        self.job('retried', attempt='ordinary-retry-2', translation=False)
        publish = Mock()
        result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result['sent'], 2)
        self.assertCountEqual(publish.call_args_list, [(('ordinary', ''),), (('retried', 'ordinary-retry-2'),)])

    def test_consumer_never_ensures_intent_from_pending_status(self):
        store = Mock()
        store.claim_dispatch.return_value = None
        result = dispatch_pending(store, Mock(), job_id='unverified-pending', now=self.now)
        self.assertEqual(result['claimed'], 0)
        store.ensure_dispatch.assert_not_called()
        store.get.assert_not_called()

    def test_partial_batch_retries_only_failed_publish_without_secrets(self):
        for name in ('a', 'b', 'c'):
            self.job(name)
        calls = []
        def publish(job_id, attempt):
            calls.append((job_id, attempt))
            if job_id == 'a':
                raise ConnectionError('redis://user:TOP_SECRET@host/private-book-text')
        result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result['claimed'], 3)
        self.assertEqual(result['sent'], 2)
        self.assertEqual(result['retry'], 1)
        self.assertEqual(result['errors'], 1)
        failed = self.record('a')
        self.assertIn('ConnectionError', failed['last_error'])
        self.assertNotIn('TOP_SECRET', failed['last_error'])
        self.assertEqual(self.store.get('a').status, JobStatus.pending)
        self.assertEqual(len(calls), 3)

    def test_backoff_is_bounded_and_survives_reopened_store(self):
        self.job()
        publish = Mock(side_effect=TimeoutError('provider details'))
        now = self.now
        delays = []
        for expected_attempt in range(1, 10):
            store = PersistentJobStore(self.engine)
            result = dispatch_pending(store, publish, job_id='job-1', now=now)
            self.assertEqual(result['retry'], 1)
            record = self.record()
            self.assertEqual(record['attempts'], expected_attempt)
            delays.append(record['next_attempt_at'] - now)
            self.assertEqual(dispatch_pending(store, publish, now=record['next_attempt_at'] - 0.01)['claimed'], 0)
            now = record['next_attempt_at']
        self.assertEqual(delays, [5, 10, 20, 40, 80, 160, 300, 300, 300])

    def test_crash_before_publish_retains_lease_then_restarts(self):
        self.job()
        publish = Mock()
        with patch.object(self.store, 'get', side_effect=SystemExit('simulated process crash')), self.assertRaises(SystemExit):
            dispatch_pending(self.store, publish, now=self.now)
        publish.assert_not_called()
        self.assertEqual(dispatch_pending(PersistentJobStore(self.engine), publish, now=self.now + 1)['claimed'], 0)
        result = dispatch_pending(PersistentJobStore(self.engine), publish, now=self.now + 61)
        self.assertEqual(result['sent'], 1)
        publish.assert_called_once_with('job-1', 'attempt-1')

    def test_publish_success_ack_error_is_not_falsely_successful(self):
        self.job()
        publish = Mock()
        with patch.object(self.store, 'finish_dispatch', side_effect=RuntimeError('db-password-secret')), self.assertLogs('epub_factory', level='WARNING') as logs:
            result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result['published'], 1)
        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['errors'], 1)
        self.assertNotIn('db-password-secret', str(logs.output))
        self.assertEqual(dispatch_pending(self.store, publish, now=self.now + 1)['claimed'], 0)
        retried = dispatch_pending(PersistentJobStore(self.engine), publish, now=self.now + 61)
        self.assertEqual(retried['sent'], 1)
        self.assertEqual(publish.call_count, 2)  # at least once, never claim exactly once

    def test_one_acknowledgement_failure_does_not_stop_other_records(self):
        self.job('a'); self.job('b')
        failing_id = self.record('a')['dispatch_id']
        original = self.store.finish_dispatch
        def finish(dispatch_id, *args, **kwargs):
            if dispatch_id == failing_id:
                raise RuntimeError('private DB details')
            return original(dispatch_id, *args, **kwargs)
        publish = Mock()
        with patch.object(self.store, 'finish_dispatch', side_effect=finish), self.assertLogs('epub_factory', level='WARNING'):
            result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result['published'], 2)
        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['errors'], 1)
        self.assertEqual(self.record('b')['status'], 'sent')

    def test_broker_accepted_then_process_crashed_republishes_after_lease(self):
        self.job()
        accepted = []
        def crashed(job_id, attempt):
            accepted.append((job_id, attempt))
            raise SystemExit('crash before acknowledgement')
        with self.assertRaises(SystemExit):
            dispatch_pending(self.store, crashed, now=self.now)
        result = dispatch_pending(PersistentJobStore(self.engine), lambda *args: accepted.append(args), now=self.now + 61)
        self.assertEqual(result['sent'], 1)
        self.assertEqual(accepted, [('job-1', 'attempt-1')] * 2)

    def test_ack_fencing_late_publisher_cannot_finish_new_claim(self):
        self.job()
        newer = []
        def publish(*_args):
            newer.append(PersistentJobStore(self.engine).claim_dispatch(now=self.now + 61))
        with self.assertLogs('epub_factory', level='WARNING'):
            result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result['published'], 1)
        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['errors'], 1)
        record = self.record()
        self.assertEqual(record['lease_token'], newer[0]['lease_token'])
        self.assertTrue(self.store.finish_dispatch(record['dispatch_id'], record['lease_token'], outcome='sent', now=self.now + 62))

    def test_cancelled_terminal_and_missing_jobs_are_obsolete(self):
        for status in (JobStatus.cancelled, JobStatus.success, JobStatus.failed, JobStatus.pending_payment,
                       JobStatus.awaiting_confirmation, JobStatus.confirming, None):
            name = status.value if status else 'missing'
            self.job(name)
            snapshot = None if status is None else SimpleNamespace(status=status, translation_stats={'attempt_id': 'attempt-1'})
            publish = Mock()
            with patch.object(self.store, 'get', return_value=snapshot):
                result = dispatch_pending(self.store, publish, job_id=name, now=self.now)
            self.assertEqual(result['obsolete'], 1)
            publish.assert_not_called()
            self.assertEqual(self.record(name)['status'], 'obsolete')

    def test_stale_attempt_is_obsolete_even_when_current_job_runs(self):
        self.job()
        snapshot = SimpleNamespace(status=JobStatus.running, translation_stats={'attempt_id': 'attempt-2'})
        publish = Mock()
        with patch.object(self.store, 'get', return_value=snapshot):
            result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result['obsolete'], 1)
        publish.assert_not_called()

    def test_current_running_job_is_acknowledged_without_resubmission(self):
        self.job()
        self.store.update_status('job-1', JobStatus.running)
        publish = Mock()
        result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['published'], 0)
        publish.assert_not_called()
        self.assertEqual(self.store.get('job-1').status, JobStatus.running)

    def test_state_read_failure_retries_and_does_not_stop_other_records(self):
        self.job('a'); self.job('b')
        original = self.store.get
        def read(name):
            if name == 'a':
                raise RuntimeError('database-secret')
            return original(name)
        publish = Mock()
        with patch.object(self.store, 'get', side_effect=read):
            result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result['retry'], 1)
        self.assertEqual(result['sent'], 1)
        self.assertNotIn('database-secret', self.record('a')['last_error'])
        publish.assert_called_once_with('b', 'attempt-1')

    def test_failed_claim_is_explicit_and_does_not_publish(self):
        publish = Mock()
        with patch.object(self.store, 'claim_dispatch', side_effect=RuntimeError('db secret')), self.assertLogs('epub_factory', level='WARNING'):
            result = dispatch_pending(self.store, publish, now=self.now)
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['sent'], 0)
        publish.assert_not_called()

    def test_pass_is_bounded_and_job_filter_is_forwarded(self):
        fake = Mock()
        fake.claim_dispatch.return_value = {'dispatch_id': 'one', 'lease_token': 'token', 'job_id': 'job', 'attempt_id': '', 'attempts': 1}
        fake.get.return_value = SimpleNamespace(status=JobStatus.pending, translation_stats={})
        fake.finish_dispatch.return_value = True
        publish = Mock()
        result = dispatch_pending(fake, publish, job_id='job', limit=100000, now=self.now)
        self.assertEqual(result['claimed'], MAX_BATCH_SIZE)
        self.assertEqual(publish.call_count, MAX_BATCH_SIZE)
        self.assertTrue(all(call.kwargs['job_id'] == 'job' for call in fake.claim_dispatch.call_args_list))
        fake.reset_mock()
        self.assertEqual(dispatch_pending(fake, publish, limit=0)['claimed'], 0)
        fake.claim_dispatch.assert_not_called()

    def test_datetime_now_supported_by_store_contract(self):
        self.job()
        result = dispatch_pending(self.store, Mock(), now=datetime.fromtimestamp(self.now, timezone.utc))
        self.assertEqual(result['sent'], 1)

    def test_worker_has_no_constructor_io_and_is_restartable(self):
        self.job()
        event = threading.Event()
        provider = Mock(return_value=self.store)
        def publish(*_args):
            event.set()
        worker = JobDispatchWorker(provider, publish, interval_seconds=60)
        self.addCleanup(worker.stop)
        provider.assert_not_called()
        self.assertTrue(worker.start())
        self.assertFalse(worker.start())
        self.assertTrue(event.wait(2))
        self.assertTrue(worker.stop())
        self.assertTrue(worker.start())
        self.assertTrue(worker.stop())
        self.assertEqual(self.record()['status'], 'sent')

    def test_worker_stop_is_bounded_when_publisher_inflight(self):
        self.job()
        entered, release = threading.Event(), threading.Event()
        def publish(*_args):
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test release not set')
        worker = JobDispatchWorker(lambda: self.store, publish)
        self.addCleanup(worker.stop)
        self.addCleanup(release.set)
        worker.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        self.assertFalse(worker.stop(timeout=0.01))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(worker.start())
        release.set()
        self.assertTrue(worker.stop())

    def test_worker_recovers_from_provider_failure_when_woken(self):
        event, resumed = threading.Event(), threading.Event()
        empty = Mock()
        empty.claim_dispatch.return_value = None
        attempts = []
        def provider():
            attempts.append(1)
            if len(attempts) == 1:
                event.set()
                raise RuntimeError('credentials in error must stay private')
            resumed.set()
            return empty
        worker = JobDispatchWorker(provider, Mock(), interval_seconds=60)
        self.addCleanup(worker.stop)
        with self.assertLogs('epub_factory', level='WARNING') as logs:
            worker.start()
            self.assertTrue(event.wait(2))
            worker.wake()
            self.assertTrue(resumed.wait(2))
            self.assertTrue(worker.stop())
        self.assertNotIn('credentials in error', str(logs.output))
        self.assertGreaterEqual(len(attempts), 2)


if __name__ == '__main__':
    unittest.main()
