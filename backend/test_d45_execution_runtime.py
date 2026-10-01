"""R7 real admission/state, local lease and heartbeat; no gateway/model I/O."""
import copy
import json
import os
import shutil
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update

import test_d43_dispatch_execution as fixtures


class ExecutionHeartbeatTests(unittest.TestCase):
    def setUp(self):
        from app.infra.execution_heartbeat import ExecutionHeartbeat
        from app.infra.execution_lease import LocalExecutionLease, ExecutionLeaseLost, RedisExecutionLease
        self.Heartbeat = ExecutionHeartbeat
        self.LocalLease, self.RedisLease, self.Lost = LocalExecutionLease, RedisExecutionLease, ExecutionLeaseLost
        self.lease = LocalExecutionLease()
        self.store = Mock()
        self.store.heartbeat_execution.return_value = True
        self.heartbeat = ExecutionHeartbeat(self.store, 'book', 'attempt-1', self.lease, interval_seconds=.05)
        self.addCleanup(self.heartbeat.stop)

    def test_constructor_and_stopped_heartbeat_have_no_store_io(self):
        self.store.heartbeat_execution.assert_not_called()
        self.assertTrue(self.heartbeat.stop())
        self.heartbeat.start()
        self.assertFalse(self.heartbeat.pulse())
        self.store.heartbeat_execution.assert_not_called()

    def test_thread_beats_independently_and_stops_without_more_calls(self):
        beaten = threading.Event()
        thread_ids = []
        def beat(*args):
            thread_ids.append(threading.get_ident())
            if len(thread_ids) >= 2:
                beaten.set()
            return True
        self.store.heartbeat_execution.side_effect = beat
        self.heartbeat.start()
        thread = self.heartbeat._thread
        self.heartbeat.start()
        self.assertIs(self.heartbeat._thread, thread)
        self.assertTrue(beaten.wait(2))
        self.assertTrue(self.heartbeat.stop())
        calls = self.store.heartbeat_execution.call_count
        self.assertFalse(self.heartbeat.pulse())
        self.assertEqual(self.store.heartbeat_execution.call_count, calls)
        self.assertTrue(all(ident != threading.get_ident() for ident in thread_ids))
        self.store.heartbeat_execution.assert_called_with('book', 'attempt-1', self.lease.owner)

    def test_owner_rejection_and_db_error_mark_lease_lost_without_error_leak(self):
        for effect in (False, RuntimeError('postgres://credentials/and-private-book-text')):
            with self.subTest(effect=type(effect).__name__):
                lease = self.LocalLease()
                store = Mock()
                if isinstance(effect, Exception):
                    store.heartbeat_execution.side_effect = effect
                else:
                    store.heartbeat_execution.return_value = effect
                heartbeat = self.Heartbeat(store, 'book', 'attempt', lease)
                with self.assertLogs('epub_factory.execution_heartbeat', level='WARNING') as logs:
                    self.assertFalse(heartbeat.pulse())
                self.assertNotIn('credentials', ''.join(logs.output))
                self.assertNotIn('private-book-text', ''.join(logs.output))
                with self.assertRaises(self.Lost):
                    lease.assert_owned()
                self.assertFalse(heartbeat.pulse())
                store.heartbeat_execution.assert_called_once()

    def test_redis_mark_lost_prevents_further_renewal(self):
        client = Mock()
        lease = self.RedisLease(client, 'key')
        lease.mark_lost()
        self.assertFalse(lease.renew())
        with self.assertRaises(self.Lost):
            lease.assert_owned()
        client.eval.assert_not_called()
        client.get.assert_not_called()
        self.assertTrue(lease._stop.is_set())

    def test_lost_transport_lease_does_not_write_durable_heartbeat(self):
        self.lease.mark_lost()
        with self.assertLogs('epub_factory.execution_heartbeat', level='WARNING'):
            self.assertFalse(self.heartbeat.pulse())
        self.store.heartbeat_execution.assert_not_called()

    def test_stop_does_not_wait_indefinitely_for_blocked_db_call(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(2)
            return True
        self.store.heartbeat_execution.side_effect = blocked
        self.heartbeat.start()
        self.assertTrue(entered.wait(2))
        try:
            start = time.monotonic()
            self.assertFalse(self.heartbeat.stop(timeout_seconds=.01))
            self.assertLess(time.monotonic() - start, .5)
        finally:
            release.set()
        self.assertTrue(self.heartbeat.stop())

    def test_engineering_interval_default_bounds_and_invalid_config(self):
        for raw, expected in (('', 15), ('15', 15), ('99', 20), ('0', 1), ('nan', 15), ('bad', 15)):
            with patch.dict(os.environ, {'JOB_EXECUTION_HEARTBEAT_SECONDS': raw}):
                self.assertEqual(self.Heartbeat(self.store, 'j', '', self.lease).interval_seconds, expected)


class ExecutionRuntimeTests(unittest.TestCase):
    _patch = fixtures.DispatchExecutionTests._patch
    tearDown = fixtures.DispatchExecutionTests.tearDown
    job = fixtures.DispatchExecutionTests.job
    convert = fixtures.DispatchExecutionTests.convert

    def setUp(self):
        fixtures.DispatchExecutionTests.setUp(self)
        from app.infra.execution_heartbeat import ExecutionHeartbeat
        from app.infra.execution_lease import ExecutionLeaseLost, execution_identity
        self.Heartbeat, self.Lost, self.identity = ExecutionHeartbeat, ExecutionLeaseLost, execution_identity
        self.begin = self._patch(patch.object(self.store, 'begin_execution', wraps=self.store.begin_execution))
        self.finish = self._patch(patch.object(self.store, 'finish_execution', wraps=self.store.finish_execution))
        self.beats = self._patch(patch.object(self.store, 'heartbeat_execution', wraps=self.store.heartbeat_execution))
        self.heartbeats = []
        def heartbeat(*args, **kwargs):
            instance = ExecutionHeartbeat(*args, **kwargs, interval_seconds=.05)
            self.heartbeats.append(instance)
            return instance
        self._patch(patch.object(self.runner, 'ExecutionHeartbeat', side_effect=heartbeat))
        self.addCleanup(lambda: [heartbeat.stop() for heartbeat in self.heartbeats])

    def assert_no_live_threads(self):
        for heartbeat in self.heartbeats:
            self.assertTrue(heartbeat._thread is None or not heartbeat._thread.is_alive())

    def test_real_running_admission_and_independent_heartbeat_during_silent_work(self):
        job = self.job()
        original = self.store.heartbeat_execution._mock_wraps
        observed = threading.Event()
        def beat(*args, **kwargs):
            result = original(*args, **kwargs)
            observed.set()
            return result
        self.beats.side_effect = beat
        def silent_work(*args, **kwargs):
            before = self.store.get(job.id)
            self.assertEqual(before.status, self.JobStatus.running)
            self.assertTrue(observed.wait(2))
            self.assertEqual(self.store.get(job.id).message, before.message)
            return self.convert(*args, **kwargs)
        self.converter.side_effect = silent_work
        self.runner.run_job(job.id, 'retry-2')
        self.begin.assert_called_once()
        self.finish.assert_called_once()
        record = self.store.get_execution(job.id, 'retry-2')
        self.assertEqual(record['state'], 'finished')
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.success)
        self.assertGreaterEqual(self.beats.call_count, 1)
        self.assert_no_live_threads()

    def test_running_duplicate_never_executes_even_after_external_lease_is_free(self):
        job = self.job()
        self.assertTrue(self.store.begin_execution(job.id, 'retry-2', 'first-owner'))
        self.begin.reset_mock()
        self.runner.run_job(job.id, 'retry-2', retry_if_busy=True)
        self.converter.assert_not_called()
        self.admission.assert_not_called()
        self.begin.assert_not_called()
        self.assertEqual(self.store.get_execution(job.id, 'retry-2')['owner'], 'first-owner')

    def test_locked_reread_rejects_concurrently_admitted_running_job(self):
        job = self.job()
        @contextmanager
        def concurrent_admission(job_id, identity):
            self.assertTrue(self.store.begin_execution(job_id, 'retry-2', 'other-owner'))
            with self.real_lease(job_id, identity) as lease:
                yield lease
        with patch.object(self.runner, 'execution_lease', side_effect=concurrent_admission):
            self.runner.run_job(job.id, 'retry-2')
        self.converter.assert_not_called()
        self.writes.assert_not_called()
        self.assertEqual(self.store.get_execution(job.id, 'retry-2')['owner'], 'other-owner')

    def test_legacy_none_caller_cannot_adopt_new_attempt_under_old_lease(self):
        job = self.job(attempt='')
        @contextmanager
        def restart_before_lock(job_id, identity):
            self.assertEqual(identity, 'conversion')
            with self.engine.begin() as connection:
                connection.execute(update(self.JobRecord).where(self.JobRecord.id == job_id).values(
                    translation_stats_json=json.dumps({'attempt_id': 'retry-new'})))
            with self.real_lease(job_id, identity) as lease:
                yield lease
        with patch.object(self.runner, 'execution_lease', side_effect=restart_before_lock):
            self.runner.run_job(job.id)  # None may only adopt the first snapshot.
        self.converter.assert_not_called()
        self.begin.assert_not_called()
        self.writes.assert_not_called()
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.pending)
        self.assertEqual(self.store.get(job.id).translation_stats['attempt_id'], 'retry-new')
        self.assertIsNone(self.store.get_execution(job.id, 'retry-new'))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_rejected_atomic_begin_does_not_start_pipeline_or_heartbeat(self):
        job = self.job()
        self.begin.side_effect = lambda *a, **k: False
        self.runner.run_job(job.id, 'retry-2')
        self.converter.assert_not_called()
        self.assertEqual(self.heartbeats, [])
        self.writes.assert_not_called()

    def test_soft_timeout_preserves_stats_and_running_record_for_bounded_recovery(self):
        job = self.job()
        self.store.update_status(job.id, job.status, translation_stats={
            'attempt_id': 'retry-2', 'cached': 19, 'translated': 23, 'checkpoint': {'kept': True}})
        expected = copy.deepcopy(self.store.get(job.id).translation_stats)
        def timeout(source, destination, *args, **kwargs):
            shutil.copyfile(source, destination)
            kwargs['progress_callback']('Book has cached progress')
            raise SoftTimeLimitExceeded()
        self.converter.side_effect = timeout
        with self.assertLogs('epub_factory', level='WARNING'), self.assertRaises(SoftTimeLimitExceeded):
            self.runner.run_job(job.id, 'retry-2')
        current = self.store.get(job.id)
        self.assertEqual(current.status, self.JobStatus.running)
        self.assertEqual(current.translation_stats, expected)
        self.assertIsNone(current.error_code)
        self.assertEqual(self.store.get_execution(job.id, 'retry-2')['state'], 'running')
        self.finish.assert_not_called()
        self.report.assert_not_called()
        self.notify.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(any(stage.stage_name == 'failed' for stage in self.store.list_stages(job.id)))
        self.assert_no_live_threads()

    def test_database_heartbeat_rejection_blocks_output_and_leaves_recoverable_running(self):
        job = self.job()
        rejected = threading.Event()
        def reject(*args, **kwargs):
            rejected.set()
            return False
        self.beats.side_effect = reject
        def delayed(*args, **kwargs):
            self.assertTrue(rejected.wait(2))
            # Wait for the heartbeat's mark_lost, not merely the mock returning.
            self.assertTrue(self.heartbeats[0]._stop.wait(2))
            return self.convert(*args, **kwargs)
        self.converter.side_effect = delayed
        with self.assertLogs('epub_factory', level='WARNING'), self.assertRaises(self.Lost):
            self.runner.run_job(job.id, 'retry-2')
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.running)
        self.assertEqual(self.store.get_execution(job.id, 'retry-2')['state'], 'running')
        self.assertEqual(list(self.output.iterdir()), [])
        self.finish.assert_not_called()
        self.notify.assert_not_called()
        self.assert_no_live_threads()

    def test_cancel_and_provider_failure_remain_terminal_and_finish_metadata(self):
        from app.infra.llm_errors import ProviderAccountUnavailable
        from app.cancellation import JobCancelled
        for index, error in enumerate((JobCancelled('用户取消'), ProviderAccountUnavailable('offline-provider'))):
            job = self.job(f'terminal-{index}')
            self.converter.side_effect = error
            with self.assertLogs('epub_factory', level='INFO'):
                self.runner.run_job(job.id, 'retry-2')
            expected = self.JobStatus.cancelled if isinstance(error, JobCancelled) else self.JobStatus.failed
            self.assertEqual(self.store.get(job.id).status, expected)
            self.assertEqual(self.store.get_execution(job.id, 'retry-2')['state'], 'finished')
        self.assertEqual(self.finish.call_count, 2)
        self.assert_no_live_threads()

    def test_unexpected_baseexception_retains_running_without_failure_or_finish(self):
        job = self.job()
        self.converter.side_effect = SystemExit('simulated process exit')
        with self.assertRaises(SystemExit):
            self.runner.run_job(job.id, 'retry-2')
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.running)
        self.assertEqual(self.store.get_execution(job.id, 'retry-2')['state'], 'running')
        self.finish.assert_not_called()
        self.notify.assert_not_called()
        self.assert_no_live_threads()

    def test_nonterminal_confirmation_or_payment_state_is_not_finished(self):
        for index, state in enumerate((self.JobStatus.pending_payment, self.JobStatus.awaiting_confirmation,
                                       self.JobStatus.confirming)):
            job = self.job(f'nonterminal-{index}')
            def changed_stage(*args, **kwargs):
                self.store.update_status(job.id, state)
            with patch.object(self.runner, '_execute_admitted_job', side_effect=changed_stage):
                self.runner.run_job(job.id, 'retry-2')
            self.assertEqual(self.store.get_execution(job.id, 'retry-2')['state'], 'running')
        self.finish.assert_not_called()
        self.assert_no_live_threads()

    def test_legacy_identity_is_stable_through_ai_initialization_and_plain_empty_stays_empty(self):
        for name, translation, precision, expected in (
            ('old-translation', True, False, 'translation-old-translation'),
            ('old-polish', False, True, 'polish-old-polish'),
            ('old-conversion', False, False, 'conversion'),
        ):
            job = self.job(name, attempt='')
            with self.engine.begin() as connection:
                connection.execute(update(self.JobRecord).where(self.JobRecord.id == name).values(
                    enable_translation=translation, enable_precision_polish=precision,
                    translation_stats_json=json.dumps({})))
            job = self.store.get(name)
            self.assertEqual(self.identity(job), expected)
            def admitted(current, attempt, stats, lease):
                reloaded = self.store.get(name)
                self.assertEqual(self.identity(reloaded), expected)
                self.assertEqual(attempt, expected if (translation or precision) else '')
                self.assertEqual(self.store.get_execution(name, attempt)['owner'], lease.owner)
                self.store.update_status(name, self.JobStatus.success)
            with patch.object(self.runner, '_execute_admitted_job', side_effect=admitted):
                self.runner.run_job(name, '')
            self.assertEqual(self.admission.call_args.args, (name, expected))
            self.assertEqual(self.store.get_execution(name, expected if (translation or precision) else '')['state'], 'finished')

    def test_book_task_does_not_unconditionally_requeue_dead_workers(self):
        from app.tasks.job_pipeline import run_conversion
        self.assertFalse(run_conversion.reject_on_worker_lost)
        self.assertTrue(run_conversion.acks_late)
        self.assertTrue(run_conversion.acks_on_failure_or_timeout)
        with patch('app.tasks.job_pipeline.run_job', side_effect=SoftTimeLimitExceeded()) as run:
            with self.assertRaises(SoftTimeLimitExceeded):
                run_conversion.run('book', expected_attempt_id='attempt')
            run.assert_called_once_with('book', expected_attempt_id='attempt', retry_if_busy=True)


if __name__ == '__main__':
    unittest.main()
