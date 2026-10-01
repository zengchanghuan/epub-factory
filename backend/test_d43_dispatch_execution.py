"""R5 queued-attempt admission using real SQLite, runner and local file lease.

Only conversion and outbound notifications are replaced. These tests establish
the two admission checks and propagation of a captured nonempty attempt; they
do not claim an atomic SQL fence for an already-running empty legacy attempt.
"""
import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, update


class DispatchExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='epub-r5-admission-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'outputs'
        self.output.mkdir()
        self._patch(patch.dict(os.environ, {
            'EPUB_PERSISTENT_STORE': '0', 'DATABASE_URL': 'sqlite:///' + str(self.root / 'bootstrap.db'),
            'CELERY_BROKER_URL': '', 'REDIS_URL': '', 'SENTRY_DSN': '',
            'NOTIFY_EMAIL_ENABLED': '0', 'OWNER_PAYMENT_EMAIL_ENABLED': '0',
        }))
        self._patch(patch('dotenv.load_dotenv', return_value=False))
        self.network = [self._patch(patch(name, side_effect=AssertionError('No R5 network/model calls')))
                        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo')]
        from app import job_runner
        from app.engine.adapters.html_to_epub_builder import build
        from app.infra import execution_lease
        from app.models import ConversionResult, Job, JobStatus, OutputMode
        from app.storage_db import Base, JobRecord, PersistentJobStore

        self.runner, self.lease_module = job_runner, execution_lease
        self.Job, self.JobStatus, self.OutputMode = Job, JobStatus, OutputMode
        self.ConversionResult, self.JobRecord = ConversionResult, JobRecord
        self.real_lease = execution_lease.execution_lease
        self.engine = create_engine('sqlite:///' + str(self.root / 'jobs.db'))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self._patch(patch.object(job_runner, 'job_store', self.store))
        self._patch(patch.object(job_runner, 'OUTPUT_DIR', self.output))
        self._patch(patch.object(execution_lease.tempfile, 'gettempdir', return_value=str(self.root)))
        self.notify = self._patch(patch.object(job_runner, 'notify_job_completed'))
        self.report = self._patch(patch.object(job_runner, 'report_error'))
        self.source = self.root / 'source.epub'
        build('<p>Original conversion input.</p>', {'title': 'Source', 'language': 'en'}, self.source)
        self.original_source = self.source.read_bytes()
        self.writes = self._patch(patch.object(self.store, 'update_status', wraps=self.store.update_status))
        self.admission = self._patch(patch.object(job_runner, 'execution_lease', wraps=self.real_lease))
        self.converter = self._patch(patch.object(job_runner.converter, 'convert_file_to_horizontal', side_effect=self.convert))

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def tearDown(self):
        self.assertEqual(self.source.read_bytes(), self.original_source)
        for network in self.network:
            network.assert_not_called()

    def job(self, name='ordinary', attempt='retry-2'):
        job = self.Job(id=name, source_filename=name + '.epub', input_path=str(self.source),
                       trace_id='trace-' + name, output_mode=self.OutputMode.simplified,
                       enable_translation=False, enable_precision_polish=False,
                       status=self.JobStatus.pending,
                       translation_stats={'attempt_id': attempt} if attempt else {})
        self.store.add(job)
        return job

    def convert(self, source, destination, _mode, **options):
        self.assertEqual(Path(source), self.source)
        self.assertFalse(options['enable_translation'])
        options['progress_callback']('Controlled conversion progress')
        options['stage_callback']('conversion', 'Controlled conversion stage', 1)
        shutil.copyfile(source, destination)
        return self.ConversionResult(message='Controlled conversion completed', validation_passed=True)

    def test_explicit_empty_old_delivery_rejected_before_lease(self):
        job = self.job(attempt='retry-2')
        self.runner.run_job(job.id, expected_attempt_id='')
        self.admission.assert_not_called()
        self.converter.assert_not_called()
        self.writes.assert_not_called()
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.pending)
        self.assertEqual(self.store.list_stages(job.id), [])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_matching_retry_admitted_through_real_lease_and_publishes_artifact(self):
        job = self.job(attempt='retry-2')
        self.runner.run_job(job.id, expected_attempt_id='retry-2')
        self.admission.assert_called_once_with(job.id, 'retry-2')
        self.converter.assert_called_once()
        current = self.store.get(job.id)
        self.assertEqual(current.status, self.JobStatus.success)
        self.assertEqual(Path(current.output_path).read_bytes(), self.original_source)
        self.assertTrue(self.notify.called)
        # The real file lock has been released, not merely replaced by a fake.
        with self.real_lease(job.id, 'retry-2') as acquired_again:
            self.assertIsNotNone(acquired_again)

    def test_attempt_changes_between_first_read_and_lease_is_rejected_when_locked(self):
        job = self.job(attempt='')
        @contextmanager
        def concurrent_restart(job_id, identity):
            self.assertEqual(identity, 'conversion')
            # Another actor commits a retry between the first read and lease.
            # The test does not invoke the runner's state-writing wrapper.
            with self.engine.begin() as connection:
                connection.execute(update(self.JobRecord).where(self.JobRecord.id == job_id).values(
                    translation_stats_json=json.dumps({'attempt_id': 'retry-new'})))
            with self.real_lease(job_id, identity) as lease:
                yield lease
        with patch.object(self.runner, 'execution_lease', side_effect=concurrent_restart) as lease:
            self.runner.run_job(job.id, expected_attempt_id='')
        lease.assert_called_once_with(job.id, 'conversion')
        self.converter.assert_not_called()
        self.writes.assert_not_called()
        current = self.store.get(job.id)
        self.assertEqual(current.status, self.JobStatus.pending)
        self.assertEqual(current.translation_stats['attempt_id'], 'retry-new')
        self.assertEqual(self.store.list_stages(job.id), [])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_legacy_none_caller_remains_compatible_for_empty_and_nonempty_attempts(self):
        for name, attempt, expected_lease in (('legacy-first', '', 'conversion'), ('legacy-retry', 'retry-2', 'retry-2')):
            with self.subTest(attempt=attempt):
                job = self.job(name, attempt=attempt)
                self.runner.run_job(job.id)  # Explicitly legacy default None.
                self.assertEqual(self.store.get(job.id).status, self.JobStatus.success)
                self.assertEqual(self.admission.call_args.args, (job.id, expected_lease))
        self.assertEqual(self.converter.call_count, 2)

    def test_ordinary_current_attempt_is_passed_to_every_status_write(self):
        job = self.job(attempt='ordinary-retry-2')
        self.runner.run_job(job.id, expected_attempt_id='ordinary-retry-2')
        self.assertGreaterEqual(self.writes.call_count, 3)  # start / progress / final
        self.assertTrue(all(call.kwargs.get('expected_attempt_id') == 'ordinary-retry-2'
                            for call in self.writes.call_args_list))
        self.assertEqual(self.writes.call_args.args[1], self.JobStatus.success)

    def test_ordinary_validation_failure_retains_nonempty_write_fence(self):
        job = self.job(attempt='ordinary-retry-2')
        original = self.convert
        def invalid(*args, **kwargs):
            result = original(*args, **kwargs)
            result.validation_passed = False
            result.message = 'Controlled validation refusal'
            return result
        self.converter.side_effect = invalid
        with self.assertLogs('epub_factory', level='WARNING'):
            self.runner.run_job(job.id, expected_attempt_id='ordinary-retry-2')
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.failed)
        self.assertTrue(all(call.kwargs.get('expected_attempt_id') == 'ordinary-retry-2'
                            for call in self.writes.call_args_list))
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
