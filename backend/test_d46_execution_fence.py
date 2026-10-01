"""R8 actual SQLite/runner races; controlled converter, no model or network.

Ownership recovery while the old Python thread is paused is deliberate fault
injection of an expired distributed lease, not a claim that flock can expire.
The final SQL write and artifact publication remain real.
"""
import asyncio
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_d43_dispatch_execution as fixtures


class ExecutionFenceTests(unittest.TestCase):
    _patch = fixtures.DispatchExecutionTests._patch
    tearDown = fixtures.DispatchExecutionTests.tearDown
    job = fixtures.DispatchExecutionTests.job
    convert = fixtures.DispatchExecutionTests.convert

    def setUp(self):
        fixtures.DispatchExecutionTests.setUp(self)
        from app.domain.job_write_fence import job_write_scope, current_job_write_fence, JobWriteConflict
        from app.domain import book_reduce_service, fast_translation_runner, chapter_translation_service
        from app.tasks import translate
        from app.models import JobChapter, JobChunk, JobStage
        self.scope, self.current_fence, self.Conflict = job_write_scope, current_job_write_fence, JobWriteConflict
        self.reducer, self.fast, self.chapter, self.task = (
            book_reduce_service, fast_translation_runner, chapter_translation_service, translate)
        self.Chapter, self.Chunk, self.Stage = JobChapter, JobChunk, JobStage
        self._patch(patch.object(self.runner, 'ExecutionHeartbeat', return_value=Mock()))
        self._patch(patch.object(self.reducer, '_REDUCE_WORK_DIR', self.root / 'reduce'))
        self._patch(patch.object(self.fast, 'job_store', self.store))
        self._patch(patch.object(self.chapter, 'job_store', self.store))
        self.raw_update = self.writes._mock_wraps

    def restart(self, job_id, new_attempt):
        job = self.store.get(job_id)
        self.raw_update(job_id, self.JobStatus.cancelled, 'User cancellation',
                        expected_attempt_id=str(job.translation_stats.get('attempt_id') or ''),
                        expected_statuses={self.JobStatus.running})
        job, reason = self.store.restart_translation_attempt(
            job_id, attempt_id=new_attempt, action_label='Retry', max_free_retries=5,
            started_at=datetime.now(timezone.utc))
        self.assertEqual(reason, 'ok')
        self.assertEqual(job.status, self.JobStatus.pending)
        return new_attempt

    def recover_owner(self, job_id, attempt):
        old = self.store.get_execution(job_id, attempt)
        self.assertEqual(self.store.recover_execution(
            job_id, attempt, old['owner'], stale_before=old['heartbeat_at'] + 1,
            now=old['heartbeat_at'] + 2), 'recovered')
        self.assertTrue(self.store.begin_execution(job_id, attempt, 'new-owner'))

    def put_new_success(self, job_id, attempt):
        destination = self.output / ('new-' + job_id + '.epub')
        destination.write_bytes(b'NEW VALIDATED EXECUTION BYTES')
        with self.scope(job_id, attempt, 'new-owner'):
            self.store.upsert_chapter(self.Chapter(job_id, 'new-chapter', 'new.xhtml'))
            self.store.upsert_chunk(self.Chunk(job_id, 'new-chapter', 'new-chunk', 0, 'p:0', 'new-hash'))
            self.raw_update(job_id, self.JobStatus.success, 'New execution complete', output_path=str(destination))
        return destination

    def paused_final_write(self, *, old_attempt, transition):
        job = self.job('race-' + transition + ('-empty' if not old_attempt else '-named'), old_attempt)
        ready, release = threading.Event(), threading.Event()
        captured = {}

        def paused_update(job_id, status, *args, **kwargs):
            if job_id == job.id and status == self.JobStatus.success:
                captured['fence'] = self.current_fence()
                captured['snapshot'] = self.store.get(job_id)
                captured['path'] = Path(kwargs['output_path'])
                ready.set()
                if not release.wait(5):
                    raise AssertionError('Test did not release old final writer')
            return self.raw_update(job_id, status, *args, **kwargs)

        self.writes.side_effect = paused_update
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(self.runner.run_job, job.id, old_attempt)
            try:
                self.assertTrue(ready.wait(5))
                self.assertEqual(captured['fence'].attempt_id, old_attempt)
                self.assertEqual(captured['snapshot'].status, self.JobStatus.running)
                self.assertIsNone(captured['snapshot'].output_path)
                self.assertTrue(captured['path'].is_file())
                self.assertEqual(captured['path'].name, job.source_filename[:-5] + '_简体.epub')
                self.assertNotEqual(captured['path'].parent, self.output)
                if transition == 'cancel':
                    self.raw_update(job.id, self.JobStatus.cancelled, 'User cancellation',
                                    expected_attempt_id=old_attempt, expected_statuses={self.JobStatus.running})
                    winner = None
                elif transition == 'retry':
                    attempt = self.restart(job.id, 'new-attempt')
                    self.assertTrue(self.store.begin_execution(job.id, attempt, 'new-owner'))
                    winner = self.put_new_success(job.id, attempt)
                else:
                    self.recover_owner(job.id, old_attempt)
                    winner = self.put_new_success(job.id, old_attempt)
                current = self.store.get(job.id)
                stages = self.store.list_stages(job.id)
            finally:
                release.set()
            old.result(timeout=5)
        final = self.store.get(job.id)
        self.assertEqual(final.status, current.status)
        self.assertEqual(final.message, current.message)
        self.assertEqual(final.translation_stats, current.translation_stats)
        self.assertEqual(final.output_path, current.output_path)
        self.assertEqual(self.store.list_stages(job.id), stages)
        self.assertFalse(captured['path'].parent.exists(), 'Only losing executor private directory is removed')
        self.notify.assert_not_called()
        self.report.assert_not_called()
        if winner:
            self.assertEqual(winner.read_bytes(), b'NEW VALIDATED EXECUTION BYTES')
            self.assertEqual([row.chapter_id for row in self.store.list_chapters(job.id)], ['new-chapter'])
            self.assertEqual([row.chunk_id for row in self.store.list_chunks(job.id)], ['new-chunk'])

    def test_late_empty_attempt_success_cannot_undo_cancel(self):
        self.paused_final_write(old_attempt='', transition='cancel')

    def test_late_named_attempt_success_cannot_undo_cancel(self):
        self.paused_final_write(old_attempt='attempt-1', transition='cancel')

    def test_late_empty_attempt_success_cannot_override_retry(self):
        self.paused_final_write(old_attempt='', transition='retry')

    def test_late_named_attempt_success_cannot_override_retry(self):
        self.paused_final_write(old_attempt='attempt-1', transition='retry')

    def test_late_empty_attempt_owner_cannot_override_same_attempt_recovery(self):
        self.paused_final_write(old_attempt='', transition='recover')

    def test_late_named_attempt_owner_cannot_override_same_attempt_recovery(self):
        self.paused_final_write(old_attempt='attempt-1', transition='recover')

    def test_same_readable_filename_never_replaces_previous_success(self):
        first = self.job('first', '')
        second = self.job('second', '')
        # Both original names are identical; two independent orders still get
        # separate exclusive execution directories, but readable download names.
        from sqlalchemy import update
        with self.engine.begin() as conn:
            conn.execute(update(self.JobRecord).where(self.JobRecord.id.in_([first.id, second.id])).values(
                source_filename='Same source.epub'))
        self.runner.run_job(first.id, '')
        first_path = Path(self.store.get(first.id).output_path)
        original = first_path.read_bytes()
        self.runner.run_job(second.id, '')
        second_path = Path(self.store.get(second.id).output_path)
        self.assertEqual(first_path.name, second_path.name)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.read_bytes(), original)
        self.assertEqual(second_path.read_bytes(), original)

    def _commit_then_raise(self, *, soft_timeout=False, read_failure=False):
        from billiard.exceptions import SoftTimeLimitExceeded
        job = self.job('uncertain-commit', '')
        captured = {}
        raw_get = self.store.get
        def update(job_id, status, *args, **kwargs):
            result = self.raw_update(job_id, status, *args, **kwargs)
            if status == self.JobStatus.success:
                captured['path'] = Path(kwargs['output_path'])
                captured['committed'] = True
                if soft_timeout:
                    raise SoftTimeLimitExceeded()
                raise RuntimeError('Controlled post-commit refresh failure')
            return result
        def get(job_id):
            if captured.get('committed') and read_failure:
                raise RuntimeError('Controlled unavailable DB after commit')
            return raw_get(job_id)
        self.writes.side_effect = update
        with patch.object(self.store, 'get', side_effect=get):
            if soft_timeout:
                with self.assertRaises(SoftTimeLimitExceeded):
                    self.runner.run_job(job.id, '')
            elif read_failure:
                with self.assertRaises(RuntimeError):
                    self.runner.run_job(job.id, '')
            else:
                self.runner.run_job(job.id, '')
        current = raw_get(job.id)
        self.assertEqual(current.status, self.JobStatus.success)
        self.assertEqual(Path(current.output_path), captured['path'])
        self.assertEqual(captured['path'].read_bytes(), self.original_source)
        self.notify.assert_not_called()
        self.report.assert_not_called()

    def test_committed_output_survives_status_refresh_failure(self):
        self._commit_then_raise()

    def test_committed_output_survives_soft_timeout_before_status_return(self):
        self._commit_then_raise(soft_timeout=True)

    def test_uncertain_commit_retains_private_output_when_db_cannot_be_read(self):
        self._commit_then_raise(read_failure=True)

    def test_uncommitted_uncertain_output_is_retained_but_never_made_downloadable(self):
        job = self.job('uncommitted-uncertain', '')
        raw_get = self.store.get
        captured = {}
        def update(job_id, status, *args, **kwargs):
            if status == self.JobStatus.success:
                captured['path'] = Path(kwargs['output_path'])
                raise RuntimeError('Controlled DB outage before commit')
            return self.raw_update(job_id, status, *args, **kwargs)
        def get(job_id):
            if captured:
                raise RuntimeError('Controlled unavailable DB')
            return raw_get(job_id)
        self.writes.side_effect = update
        with patch.object(self.store, 'get', side_effect=get), self.assertRaises(RuntimeError):
            self.runner.run_job(job.id, '')
        current = raw_get(job.id)
        self.assertEqual(current.status, self.JobStatus.running)
        self.assertIsNone(current.output_path)
        self.assertTrue(captured['path'].is_file())
        self.notify.assert_not_called()

    def test_async_tasks_and_to_thread_keep_original_owner_scope(self):
        job = self.job('context', 'attempt-1')
        self.assertTrue(self.store.begin_execution(job.id, 'attempt-1', 'old-owner'))
        ready, release = threading.Event(), threading.Event()

        async def work():
            async def child():
                snapshot = self.store.get(job.id)
                self.assertEqual(snapshot.status, self.JobStatus.running)
                self.assertEqual(self.current_fence().execution_owner, 'old-owner')
                ready.set()
                await asyncio.to_thread(release.wait, 5)
                self.assertEqual(await asyncio.to_thread(lambda: self.current_fence().execution_owner), 'old-owner')
                self.store.add_stage(self.Stage(job.id, 'obsolete-child'))
            await asyncio.create_task(child())

        def old_writer():
            with self.scope(job.id, 'attempt-1', 'old-owner'):
                with self.assertRaises(self.Conflict):
                    asyncio.run(work())
            self.assertIsNone(self.current_fence())

        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(old_writer)
            try:
                self.assertTrue(ready.wait(5))
                self.recover_owner(job.id, 'attempt-1')
            finally:
                release.set()
            old.result(timeout=5)
        self.assertEqual(self.store.list_stages(job.id), [])
        self.assertEqual(self.store.get_execution(job.id, 'attempt-1')['owner'], 'new-owner')

    def test_same_attempt_late_reduce_write_cannot_replace_new_owner_chapter(self):
        old_get = self.reducer.make_get_chapter_content('book', attempt_id='attempt', execution_owner='old')
        new_get = self.reducer.make_get_chapter_content('book', attempt_id='attempt', execution_owner='new')
        self.reducer.set_chapter_output('book', 'Text/chapter.xhtml', b'new',
                                        attempt_id='attempt', execution_owner='new')
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(self.reducer.set_chapter_output, 'book', 'Text/chapter.xhtml', b'old-late',
                        attempt_id='attempt', execution_owner='old').result()
        self.assertEqual(old_get('Text/chapter.xhtml'), b'old-late')
        self.assertEqual(new_get('Text/chapter.xhtml'), b'new')
        self.assertIsNone(self.reducer.get_chapter_output('book', 'Text/chapter.xhtml', attempt_id='attempt'))
        self.reducer.set_chapter_output('book', 'Text/chapter.xhtml', b'legacy', attempt_id='attempt')
        self.assertEqual(new_get('Text/chapter.xhtml'), b'new')

    def test_owner_validation_and_envelope_mismatch_fail_closed(self):
        for owner in ('', '../escape', 'a/b', 'a\\b', 'a' * 129):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.reducer.set_chapter_output('book', 'chapter.xhtml', b'body',
                                                attempt_id='attempt', execution_owner=owner)
        path = self.reducer.set_chapter_output('book', 'chapter.xhtml', b'body',
                                                attempt_id='attempt', execution_owner='owner')
        payload = json.loads(path.read_text())
        payload['execution_owner'] = 'other-owner'
        path.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):
            self.reducer.get_chapter_output('book', 'chapter.xhtml', attempt_id='attempt', execution_owner='owner')

    def test_chapter_task_rejects_missing_or_stale_owner_before_model(self):
        job = self.job('chapter-task', 'attempt-1')
        self.assertTrue(self.store.begin_execution(job.id, 'attempt-1', 'current-owner'))
        with patch.object(self.task, 'translate_chapter') as translate:
            for attempt, owner in ((None, None), ('attempt-1', None), (None, 'current-owner'),
                                   ('attempt-1', 'old-owner'), ('old-attempt', 'current-owner')):
                with self.subTest(attempt=attempt, owner=owner), self.assertRaises(self.Conflict):
                    self.task.translate_chapter_task.run(job.id, 'chapter', attempt, owner)
            translate.assert_not_called()
        self.assertFalse((self.root / 'reduce').exists())

    def test_chapter_task_result_is_bound_to_admitted_owner(self):
        job = self.job('chapter-good', 'attempt-1')
        self.assertTrue(self.store.begin_execution(job.id, 'attempt-1', 'owner-1'))
        result = SimpleNamespace(job_id=job.id, chapter_id='chapter', file_path='Text/chapter.xhtml',
                                 chapter_kind='body', skipped=False, error=None, chunks=[], reduced_html=b'<p>result</p>')
        def translate(*args):
            self.assertEqual(self.current_fence().execution_owner, 'owner-1')
            return result
        with patch.object(self.task, 'translate_chapter', side_effect=translate):
            self.task.translate_chapter_task.run(job.id, 'chapter', 'attempt-1', 'owner-1')
        self.assertEqual(self.reducer.get_chapter_output(job.id, result.file_path,
                         attempt_id='attempt-1', execution_owner='owner-1'), result.reduced_html)
        self.assertIsNone(self.reducer.get_chapter_output(job.id, result.file_path, attempt_id='attempt-1'))


class FastExecutionFenceTests(unittest.TestCase):
    """Real fast scheduler/translator/QA/checkpoint with only SDK transport fake."""
    def setUp(self):
        import test_d45_execution_resume as resume
        resume.ExecutionResumeTests.setUp(self)
        self.resume_helpers = resume.ExecutionResumeTests
        from app.domain.job_write_fence import job_write_scope, current_job_write_fence, JobWriteConflict
        self.scope, self.current_fence, self.Conflict = job_write_scope, current_job_write_fence, JobWriteConflict
        self.interrupt_beta = False
        self.assertTrue(self.store.begin_execution(self.job.id, 'attempt-1', 'old-owner', now=100))

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def tearDown(self):
        for network in self.network:
            network.assert_not_called()

    def run_manifest(self):
        return self.resume_helpers.run_manifest(self)

    def test_actual_fast_reduce_uses_captured_owner(self):
        with self.scope(self.job.id, 'attempt-1', 'old-owner'):
            stats, _ = self.run_manifest()
        self.assertEqual(stats['failed_chunks'], 0)
        self.assertEqual(len(self.calls), 2)
        for chapter in self.manifest['chapters']:
            if chapter.get('chapter_kind') != 'body':
                continue
            result = self.reducer.get_chapter_output(self.job.id, chapter['file_path'],
                           attempt_id='attempt-1', execution_owner='old-owner')
            self.assertIsNotNone(result)
            self.assertIsNone(self.reducer.get_chapter_output(self.job.id, chapter['file_path'], attempt_id='attempt-1'))

    def _lose_owner_after_response(self, cancel):
        client = self.previous.SemanticsTranslator._get_client.return_value
        original = client.chat.completions.create

        def transition():
            self.assertIsNone(self.current_fence(), 'Independent control actor must not inherit old scope')
            if cancel:
                self.store.update_status(self.job.id, self.Status.cancelled, 'User cancelled',
                                         expected_attempt_id='attempt-1', expected_statuses={self.Status.running})
            else:
                self.assertEqual(self.store.recover_execution(self.job.id, 'attempt-1', 'old-owner',
                                  stale_before=200, now=300), 'recovered')
                self.assertTrue(self.store.begin_execution(self.job.id, 'attempt-1', 'new-owner'))
                with self.scope(self.job.id, 'attempt-1', 'new-owner'):
                    self.store.update_status(self.job.id, self.Status.running, 'New owner progress',
                                              translation_stats={'owner_marker': 'new'})

        async def request(**options):
            self.assertEqual(self.current_fence().execution_owner, 'old-owner')
            response = await original(**options)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(transition).result(timeout=5)
            return response

        client.chat.completions.create = request
        with self.scope(self.job.id, 'attempt-1', 'old-owner'), self.assertRaises(self.Conflict):
            self.run_manifest()
        current = self.store.get(self.job.id)
        self.assertEqual(current.message, 'User cancelled' if cancel else 'New owner progress')
        self.assertEqual(current.status, self.Status.cancelled if cancel else self.Status.running)
        self.assertEqual(self.store.list_chunks(self.job.id), [])
        self.assertTrue(all(chapter.chunk_success == 0 for chapter in self.store.list_chapters(self.job.id)))
        self.assertIsNone(current.output_path)
        # In-flight real usage is a durable fact, not a mutable progress row:
        # rejecting stale state must not discard the completed provider charge.
        self.assertEqual(len(self.calls), 1)
        from app.infra.llm_usage_ledger import UsageRequest
        from sqlalchemy.orm import Session
        with Session(self.engine) as session:
            usage = session.query(UsageRequest).all()
            self.assertEqual(len(usage), 1)
            self.assertEqual(usage[0].request_status, 'response')
            self.assertEqual(usage[0].total_tokens, 120)
            self.assertEqual(usage[0].attempt_id, 'attempt-1')

    def test_actual_fast_response_after_same_attempt_recovery_cannot_write_progress(self):
        self._lose_owner_after_response(cancel=False)

    def test_actual_fast_response_after_cancel_cannot_write_progress(self):
        self._lose_owner_after_response(cancel=True)


if __name__ == '__main__':
    unittest.main()
