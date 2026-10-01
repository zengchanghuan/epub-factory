"""R10 bounded threads and actual cross-process flocks, with no network."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Isolated repair condition timed out")


def process_worker(root, concurrency, jobs, marker, mode):
    from app.domain.repair_repository import RepairRepository
    from app.domain.repair_executor import RepairExecutor
    root = Path(root)
    repository = RepairRepository(root)
    executor = RepairExecutor(int(concurrency))
    jobs = json.loads(jobs)

    def runner(repo, job_id, owner):
        metrics = root / "metrics.json"
        with repo.lock("test-metrics"):
            data = json.loads(metrics.read_text()) if metrics.exists() else {"active": 0, "peak": 0, "entries": {}}
            data['active'] += 1
            data['peak'] = max(data['peak'], data['active'])
            data['entries'][job_id] = data['entries'].get(job_id, 0) + 1
            temporary = root / '.metrics.tmp'
            temporary.write_text(json.dumps(data))
            temporary.replace(metrics)
        (root / ("entered-" + job_id)).write_text(owner)
        try:
            wait_for(lambda: (root / 'release').exists(), timeout=45)
            with repo.transaction(job_id) as order:
                order.update(status='repaired', execution_owner=owner)
        finally:
            with repo.lock("test-metrics"):
                data = json.loads(metrics.read_text())
                data['active'] -= 1
                temporary = root / '.metrics.tmp'
                temporary.write_text(json.dumps(data))
                temporary.replace(metrics)

    (root / (marker + '-ready')).write_text('ready')
    wait_for(lambda: (root / 'start').exists())
    accepted = [executor.submit(repository, job, runner) for job in jobs]
    (root / (marker + '-admitted')).write_text(json.dumps(accepted))
    if mode == 'drain':
        deadline = time.monotonic() + 45
        while any(repository.get(job)['status'] != 'repaired' for job in jobs):
            if time.monotonic() > deadline:
                raise AssertionError('Paid repair queue did not drain')
            for job in jobs:
                executor.submit(repository, job, runner)
            time.sleep(.02)
    executor.shutdown(wait=True)


class RepairExecutorTests(unittest.TestCase):
    def setUp(self):
        from app.domain.repair_repository import RepairRepository
        from app.domain.repair_executor import RepairExecutor
        self.Executor = RepairExecutor
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='r10-executor-')))
        self.repository = RepairRepository(self.root)
        self.executors = []
        self.processes = []
        self.addCleanup(self.cleanup_execution)
        self.network = [self.stack.enter_context(patch(name, side_effect=AssertionError('R10 forbids network')))
                        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo')]

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    def cleanup_execution(self):
        (self.root / 'release').touch()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        for executor in self.executors:
            executor.shutdown(wait=True)

    def executor(self, concurrency=1):
        executor = self.Executor(concurrency)
        self.executors.append(executor)
        return executor

    def paid(self, index=1, *, status='paid'):
        job_id = f'{index:032x}'
        with self.repository.transaction(job_id, create=True) as order:
            order.update(status=status, filename='original.epub', expected_amount='2.99')
        return job_id

    def held_runner(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def run(repo, job_id, owner):
            calls.append((job_id, owner))
            entered.set()
            if not release.wait(10):
                raise AssertionError('Controlled runner not released')
        return run, entered, release, calls

    def start_process(self, jobs, *, concurrency=1, marker='worker', mode='once'):
        output = self.stack.enter_context((self.root / (marker + '.log')).open('wb'))
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'process-worker',
                                    str(self.root), str(concurrency), json.dumps(jobs), marker, mode],
                                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                                   stdout=output, stderr=subprocess.STDOUT)
        self.processes.append(process)
        wait_for(lambda: (self.root / (marker + '-ready')).exists())
        return process

    def test_configuration_limits_closed_property_and_no_admission_after_shutdown(self):
        for invalid in (0, -1, 5, True, 1.0, '1'):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.Executor(invalid)
        executor = self.executor()
        self.assertFalse(executor.closed)
        executor.shutdown(wait=False)
        self.assertTrue(executor.closed)
        self.assertFalse(executor.submit(self.repository, self.paid(), Mock()))
        self.assertFalse(executor.run_inline(self.repository, self.paid(2), Mock()))

    def test_unpaid_invalid_and_unreadable_metadata_never_run_and_release_capacity(self):
        executor, runner = self.executor(), Mock()
        for status in ('pending_payment', 'repaired', 'failed', 'cancelled'):
            self.assertFalse(executor.submit(self.repository, self.paid(status=status), runner))
        for job_id in ('../outside', '', 'a' * 31, 'G' * 32):
            self.assertFalse(executor.submit(self.repository, job_id, runner))
        job_id = self.paid()
        with patch.object(self.repository, 'get', side_effect=ValueError('secret metadata')):
            with self.assertLogs('epub_factory.repair_executor', level='WARNING') as logs:
                self.assertFalse(executor.submit(self.repository, job_id, runner))
            self.assertNotIn('secret metadata', ''.join(logs.output))
        runner.assert_not_called()
        self.assertTrue(executor.run_inline(self.repository, job_id, runner))
        runner.assert_called_once()

    def test_same_job_and_global_slots_are_shared_by_separate_instances(self):
        first, second = self.executor(), self.executor()
        job_id, other = self.paid(), self.paid(2)
        runner, entered, release, calls = self.held_runner()
        self.assertTrue(first.submit(self.repository, job_id, runner))
        self.assertTrue(entered.wait(2))
        self.assertFalse(second.submit(self.repository, job_id, Mock()))
        self.assertFalse(second.submit(self.repository, other, Mock()))
        release.set()
        first.shutdown()
        completed = Mock()
        self.assertTrue(second.run_inline(self.repository, other, completed))
        completed.assert_called_once()
        self.assertEqual(len(calls), 1)
        self.assertRegex(calls[0][1], r'^[a-f0-9]{32}$')

    def test_local_future_queue_is_bounded_before_thread_submission(self):
        executor = self.executor(2)
        jobs = [self.paid(index) for index in range(1, 25)]
        runner, entered, release, _ = self.held_runner()
        with patch.object(executor._pool, 'submit', wraps=executor._pool.submit) as submit:
            self.assertTrue(executor.submit(self.repository, jobs[0], runner))
            self.assertTrue(executor.submit(self.repository, jobs[1], runner))
            started = time.monotonic()
            for job in jobs[2:]:
                self.assertFalse(executor.submit(self.repository, job, runner))
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(submit.call_count, 2)
            self.assertLessEqual(executor._pool._work_queue.qsize(), 2)
        release.set()
        executor.shutdown()

    def test_runner_and_thread_submission_exceptions_release_both_locks(self):
        executor = self.executor()
        job_id = self.paid()
        with patch.object(executor._pool, 'submit', side_effect=RuntimeError('secret executor')):
            self.assertFalse(executor.submit(self.repository, job_id, Mock()))
        self.assertTrue(executor.closed)
        executor = self.executor()
        self.assertTrue(executor.run_inline(self.repository, job_id, Mock(side_effect=ValueError('secret runner'))))
        success = Mock()
        self.assertTrue(executor.run_inline(self.repository, job_id, success))
        success.assert_called_once()

    def test_real_native_thread_start_failure_retires_pool_and_never_runs_rejected_item(self):
        executor, job_id, rejected = self.executor(), self.paid(), Mock()
        with patch('threading.Thread.start', side_effect=RuntimeError('native thread creation failed')):
            self.assertFalse(executor.submit(self.repository, job_id, rejected))
        self.assertTrue(executor.closed)
        for _ in range(50):
            self.assertFalse(executor.submit(self.repository, job_id, rejected))
        replacement, accepted = self.executor(), Mock()
        self.assertTrue(replacement.run_inline(self.repository, job_id, accepted))
        accepted.assert_called_once()
        rejected.assert_not_called()
        self.assertEqual(self.repository.get(job_id)['status'], 'paid')

    def test_existing_worker_discards_rejected_orphan_after_second_thread_start_failure(self):
        executor = self.executor(2)
        first, second = self.paid(1), self.paid(2)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def runner(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError('Accepted sibling was not released')
        self.assertTrue(executor.submit(self.repository, first, runner))
        self.assertTrue(entered.wait(5))
        rejected = Mock()
        with patch('threading.Thread.start', side_effect=RuntimeError('second native start failed')):
            self.assertFalse(executor.submit(self.repository, second, rejected))
        self.assertTrue(executor.closed)
        release.set()
        executor.shutdown(wait=True)
        rejected.assert_not_called()
        for key in (first, second):
            with self.repository.lock('execution-job-' + key, blocking=False) as acquired:
                self.assertTrue(acquired)
        replacement, accepted = self.executor(2), Mock()
        self.assertTrue(replacement.run_inline(self.repository, second, accepted))
        accepted.assert_called_once()

    def test_fresh_state_or_metadata_failure_after_admission_cannot_run(self):
        executor, runner = self.executor(), Mock()
        job_id = self.paid()
        for later in ({'status': 'cancelled'}, {'status': 'repaired'}, None, ValueError('private metadata')):
            with self.subTest(later=type(later).__name__), \
                 patch.object(self.repository, 'get', side_effect=[{'status': 'paid'}, later]):
                self.assertTrue(executor.run_inline(self.repository, job_id, runner))
            runner.assert_not_called()
        self.assertTrue(executor.run_inline(self.repository, job_id, runner))
        runner.assert_called_once()

    def test_preheld_job_or_slot_returns_without_waiting_or_submitting_a_thread(self):
        executor, job_id = self.executor(), self.paid()
        for name in ('execution-job-' + job_id, 'execution-slot-0'):
            with self.subTest(lock=name), self.repository.lock(name) as held, \
                 patch.object(executor._pool, 'submit') as submit:
                self.assertTrue(held)
                start = time.monotonic()
                self.assertFalse(executor.submit(self.repository, job_id, Mock()))
                self.assertLess(time.monotonic() - start, .5)
                submit.assert_not_called()
        self.assertTrue(executor.run_inline(self.repository, job_id, Mock()))

    def test_inline_uses_same_global_admission_and_waits_for_completion(self):
        first, second = self.executor(), self.executor()
        job_id, other = self.paid(), self.paid(2)
        runner, entered, release, _ = self.held_runner()
        results = []
        thread = threading.Thread(target=lambda: results.append(first.run_inline(self.repository, job_id, runner)))
        thread.start()
        self.addCleanup(lambda: thread.join(5))
        self.assertTrue(entered.wait(2))
        self.assertEqual(results, [])
        self.assertFalse(second.run_inline(self.repository, other, Mock()))
        release.set()
        thread.join(3)
        self.assertEqual(results, [True])

    def test_configuration_mismatch_corruption_and_symlink_fail_closed(self):
        first, second = self.executor(), self.executor(2)
        job_id = self.paid()
        self.assertTrue(first.run_inline(self.repository, job_id, Mock()))
        with self.assertLogs('epub_factory.repair_executor', level='WARNING') as logs:
            self.assertFalse(second.submit(self.repository, job_id, Mock()))
        self.assertIn('configuration mismatch', ''.join(logs.output))
        config = self.root / '.repair-executor.json'
        config.write_text('{broken')
        with self.assertLogs('epub_factory.repair_executor', level='WARNING') as logs:
            self.assertFalse(first.submit(self.repository, job_id, Mock()))
        self.assertIn('configuration unavailable', ''.join(logs.output))
        config.write_text('{"version": true, "concurrency": 1}')
        self.assertFalse(first.submit(self.repository, job_id, Mock()))
        config.unlink()
        outside = self.root / 'outside.txt'
        outside.write_text('untouched')
        config.symlink_to(outside)
        self.assertFalse(first.submit(self.repository, job_id, Mock()))
        self.assertEqual(outside.read_text(), 'untouched')

    def test_initial_configuration_syncs_file_then_rename_then_directory_before_runner(self):
        import stat
        executor, job_id = self.executor(), self.paid()
        # Pre-create the durable lock so only config publication is observed.
        with self.repository.lock('execution-config'):
            pass
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def sync(fd):
            events.append('directory-sync' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file-sync')
            return real_fsync(fd)

        def replace(source, destination, *args, **kwargs):
            result = real_replace(source, destination, *args, **kwargs)
            events.append('replace')
            return result

        with patch('app.domain.repair_executor.os.fsync', side_effect=sync), \
             patch('app.domain.repair_executor.os.replace', side_effect=replace):
            self.assertTrue(executor.run_inline(self.repository, job_id, lambda *args: events.append('runner')))
        self.assertEqual(events, ['file-sync', 'replace', 'directory-sync', 'runner'])

    def test_configuration_directory_sync_failure_refuses_execution_and_releases_capacity(self):
        import stat
        executor, job_id, runner = self.executor(), self.paid(), Mock()
        with self.repository.lock('execution-config'):
            pass
        real_fsync = os.fsync

        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('sensitive filesystem detail')
            return real_fsync(fd)

        with patch('app.domain.repair_executor.os.fsync', side_effect=sync), \
             self.assertLogs('epub_factory.repair_executor', level='WARNING') as logs:
            self.assertFalse(executor.submit(self.repository, job_id, runner))
        runner.assert_not_called()
        self.assertNotIn('sensitive filesystem detail', ''.join(logs.output))
        self.assertFalse(list(self.root.glob('.repair-executor-*.tmp')))
        self.assertEqual(json.loads((self.root / '.repair-executor.json').read_text()),
                         {'version': 1, 'concurrency': 1})
        self.assertTrue(executor.run_inline(self.repository, job_id, runner))
        runner.assert_called_once()

    def test_shutdown_nonblocking_keeps_live_execution_locked_until_finished(self):
        executor, other = self.executor(), self.executor()
        job_id, next_job = self.paid(), self.paid(2)
        runner, entered, release, calls = self.held_runner()
        self.assertTrue(executor.submit(self.repository, job_id, runner))
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        executor.shutdown(wait=False)
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(executor.closed)
        self.assertFalse(other.submit(self.repository, next_job, Mock()))
        release.set()
        executor.shutdown(wait=True)
        self.assertTrue(other.run_inline(self.repository, next_job, Mock()))
        self.assertEqual(len(calls), 1)

    def test_shutdown_waits_for_admitted_runner_without_killing_it(self):
        executor = self.executor()
        runner, entered, release, _ = self.held_runner()
        self.assertTrue(executor.submit(self.repository, self.paid(), runner))
        self.assertTrue(entered.wait(2))
        done = threading.Event()
        closer = threading.Thread(target=lambda: (executor.shutdown(wait=True), done.set()))
        closer.start()
        self.addCleanup(lambda: closer.join(5))
        self.assertFalse(done.wait(.1))
        release.set()
        self.assertTrue(done.wait(3))

    def test_real_processes_obey_shared_cap_and_drain_many_paid_orders_once(self):
        # Each N uses a new shared directory: an existing directory deliberately
        # refuses to silently change its global concurrency configuration.
        from app.domain.repair_repository import RepairRepository
        for concurrency in (1, 2):
            with self.subTest(concurrency=concurrency):
                original = self.root
                self.root = original / ('cap-' + str(concurrency))
                self.root.mkdir()
                self.repository = RepairRepository(self.root)
                jobs = [self.paid(index) for index in range(1, 13)]
                first = self.start_process(jobs, concurrency=concurrency, marker='first', mode='drain')
                second = self.start_process(jobs, concurrency=concurrency, marker='second', mode='drain')
                (self.root / 'start').touch()
                wait_for(lambda: (self.root / 'metrics.json').exists() and
                         json.loads((self.root / 'metrics.json').read_text())['active'] == concurrency)
                (self.root / 'release').touch()
                first.wait(timeout=20)
                second.wait(timeout=20)
                self.assertEqual(first.returncode, 0, (self.root / 'first.log').read_text())
                self.assertEqual(second.returncode, 0, (self.root / 'second.log').read_text())
                metrics = json.loads((self.root / 'metrics.json').read_text())
                self.assertEqual(metrics['peak'], concurrency)
                self.assertEqual(metrics['active'], 0)
                self.assertEqual(metrics['entries'], {job: 1 for job in jobs})
                self.assertTrue(all(self.repository.get(job)['status'] == 'repaired' for job in jobs))
                self.root = original

    def test_process_death_releases_job_and_slot_without_unlinking_lock_files(self):
        job_id = self.paid()
        first = self.start_process([job_id], marker='killed')
        (self.root / 'start').touch()
        wait_for(lambda: (self.root / ('entered-' + job_id)).exists())
        lock_inodes = {path.name: path.stat().st_ino for path in (self.root / '.repair-locks').glob('execution-*.lock')}
        self.assertTrue(lock_inodes)
        first.kill()
        first.wait(timeout=5)
        self.assertEqual(self.repository.get(job_id)['status'], 'paid')
        completed = Mock()
        self.assertTrue(self.executor().run_inline(self.repository, job_id, completed))
        completed.assert_called_once()
        self.assertEqual({path.name: path.stat().st_ino for path in (self.root / '.repair-locks').glob('execution-*.lock')}, lock_inodes)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'process-worker':
        with ExitStack() as guards:
            for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
                guards.enter_context(patch(target, side_effect=AssertionError('R10 subprocess forbids network')))
            process_worker(*sys.argv[2:])
    else:
        unittest.main(verbosity=2)
