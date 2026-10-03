"""Real SQLite/runner PDF lifecycle; only bounded PDF engine and messages mocked."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import unittest

from billiard.exceptions import SoftTimeLimitExceeded

import test_d43_dispatch_execution as execution_fixture
import test_d59_pdf_product as product_fixture


class PdfExecutionTests(unittest.TestCase):
    _patch = execution_fixture.DispatchExecutionTests._patch
    tearDown = execution_fixture.DispatchExecutionTests.tearDown
    convert = execution_fixture.DispatchExecutionTests.convert

    def setUp(self):
        execution_fixture.DispatchExecutionTests.setUp(self)
        from app.domain import pdf_product
        from app.domain.checkout_resume import checkout_snapshot
        from app.storage_db import PersistentJobStore
        self.product, self.checkout_snapshot, self.Store = pdf_product, checkout_snapshot, PersistentJobStore
        self.pdf = self.root / '原文書.pdf'; self.pdf.write_bytes(product_fixture.SOURCE)
        self._patch(patch.object(pdf_product, '_versions', side_effect=lambda jar: product_fixture.versions()))
        self.parser = self._patch(patch.object(pdf_product.pdf_conversion, 'convert_text_pdf', side_effect=self.parse))

    def parse(self, source, destination, **kwargs):
        self.assertEqual(Path(source).read_bytes(), product_fixture.SOURCE)
        Path(destination).write_bytes(product_fixture.ARTIFACT)
        return product_fixture.report()

    def job(self, name='pdf-job'):
        job = self.Job(id=name, source_filename='原文書.pdf', input_path=str(self.pdf), trace_id='trace-' + name,
                       output_mode=self.OutputMode.original, expected_amount='0.99', status=self.JobStatus.pending,
                       translation_stats={'attempt_id': 'prepare-' + name, 'pdf_conversion': self.product.new_pdf_plan(
                           product_fixture.hashlib.sha256(product_fixture.SOURCE).hexdigest(), len(product_fixture.SOURCE), '0.99')})
        self.store.add(job)
        return job

    def prepare(self, name='pdf-job'):
        job = self.job(name)
        self.runner.run_job(job.id, job.translation_stats['attempt_id'])
        return self.store.get(job.id)

    def pay(self, job):
        plan = job.translation_stats['pdf_conversion']
        confirmed = self.product.confirm_pdf_plan(job, self.output, plan['plan_id'], [])
        claimed = self.store.begin_pdf_confirmation(job.id, plan_id=plan['plan_id'], confirmed_plan=confirmed)
        self.assertIsNotNone(claimed)
        self.assertNotEqual(claimed.translation_stats['attempt_id'], job.translation_stats['attempt_id'])
        checked = self.checkout_snapshot(job.id, job.expected_amount, pay_url='https://checkout.invalid/order',
                                        created_at=datetime.now(timezone.utc))
        saved = self.store.finish_pdf_confirmation(job.id, attempt_id=claimed.translation_stats['attempt_id'],
            status=self.JobStatus.pending_payment, message='Awaiting payment', payment_checkout=checked)
        self.assertIsNotNone(saved)
        result = self.store.settle_verified_payment(job.id, amount='0.99', source='verified_webhook')
        self.assertEqual(result['released'], [job.id])
        return self.store.get(job.id)

    def test_preparation_commits_private_plan_and_execution_without_delivery(self):
        job = self.prepare()
        self.assertEqual(job.status, self.JobStatus.awaiting_confirmation)
        self.assertIsNone(job.output_path)
        self.assertEqual(self.store.get_execution(job.id, job.translation_stats['attempt_id'])['state'], 'finished')
        self.product.validate_pdf_job(job, self.output)
        self.assertEqual(self.pdf.read_bytes(), product_fixture.SOURCE)
        self.converter.assert_not_called(); self.notify.assert_not_called()
        self.assertFalse(list(self.output.glob('.execution-*')))

    def test_store_reload_and_new_delivery_attempt_keep_exact_prepared_bytes(self):
        prepared = self.prepare()
        reloaded = self.Store(self.engine).get(prepared.id)
        self.assertEqual(reloaded.translation_stats, prepared.translation_stats)
        job = self.pay(reloaded)
        first_attempt = prepared.translation_stats['attempt_id']
        final_attempt = job.translation_stats['attempt_id']
        self.runner.run_job(job.id, first_attempt)  # A duplicate old broker message.
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.pending)
        self.runner.run_job(job.id, final_attempt)
        delivered = self.store.get(job.id)
        self.assertEqual(delivered.status, self.JobStatus.success)
        self.assertEqual(Path(delivered.output_path).read_bytes(), product_fixture.ARTIFACT)
        self.assertEqual(Path(delivered.output_path).name, '原文書_原文.epub')
        self.assertEqual(self.parser.call_count, 1)
        self.converter.assert_not_called()
        self.assertEqual(delivered.translation_stats['pdf_conversion']['plan_id'], prepared.translation_stats['pdf_conversion']['plan_id'])
        self.assertEqual(len(self.store.list_dispatches(job.id)), 2)
        self.runner.run_job(job.id, final_attempt)
        self.assertEqual(self.parser.call_count, 1)
        self.notify.assert_called_once()

    def test_failed_preparation_cannot_create_download_or_payment(self):
        job = self.job()
        self.parser.side_effect = self.product.pdf_conversion.PdfConversionError('layout_unsupported')
        with self.assertLogs('epub_factory', level='ERROR'):
            self.runner.run_job(job.id, job.translation_stats['attempt_id'])
        saved = self.store.get(job.id)
        self.assertEqual(saved.status, self.JobStatus.failed)
        self.assertIsNone(saved.output_path)
        self.assertEqual(saved.payment_resolution, {})
        self.assertNotIn('payment_checkout', saved.translation_stats)
        self.converter.assert_not_called()

    def test_missing_descriptor_original_mode_does_not_fall_into_cjk(self):
        job = self.job()
        self.store.update_status(job.id, self.JobStatus.pending, translation_stats={'pdf_conversion': None})
        with self.assertLogs('epub_factory', level='ERROR'):
            self.runner.run_job(job.id, job.translation_stats['attempt_id'])
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.failed)
        self.parser.assert_not_called(); self.converter.assert_not_called()

    def test_delivery_without_verified_payment_fails_despite_pending_status(self):
        prepared = self.prepare()
        plan = prepared.translation_stats['pdf_conversion']
        confirmed = self.product.confirm_pdf_plan(prepared, self.output, plan['plan_id'], [])
        claimed = self.store.begin_pdf_confirmation(prepared.id, plan_id=plan['plan_id'], confirmed_plan=confirmed)
        # Fault injection of an unauthorized queued status is not a receipt.
        self.store.update_status(claimed.id, self.JobStatus.pending)
        with self.assertLogs('epub_factory', level='ERROR'):
            self.runner.run_job(claimed.id, claimed.translation_stats['attempt_id'])
        saved = self.store.get(claimed.id)
        self.assertEqual(saved.status, self.JobStatus.failed)
        self.assertIsNone(saved.output_path)
        self.assertEqual(self.parser.call_count, 1)

    def test_changed_prepared_artifact_prevents_paid_delivery(self):
        job = self.pay(self.prepare())
        self.product._artifact_path(job.translation_stats['pdf_conversion'], self.output).write_bytes(b'changed')
        with self.assertLogs('epub_factory', level='ERROR'):
            self.runner.run_job(job.id, job.translation_stats['attempt_id'])
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.failed)
        self.assertIsNone(self.store.get(job.id).output_path)
        self.assertEqual(self.parser.call_count, 1)

    def test_preparation_commit_then_exception_retains_persisted_artifact(self):
        job = self.job()
        finish = self.store.finish_pdf_preparation
        def commit_then_raise(*args):
            saved = finish(*args)
            self.assertIsNotNone(saved)
            raise RuntimeError('Simulated transport failure after commit')
        with patch.object(self.store, 'finish_pdf_preparation', side_effect=commit_then_raise):
            self.runner.run_job(job.id, job.translation_stats['attempt_id'])
        saved = self.store.get(job.id)
        self.assertEqual(saved.status, self.JobStatus.awaiting_confirmation)
        self.product.validate_pdf_job(saved, self.output)
        self.assertIsNone(saved.output_path)
        self.notify.assert_not_called(); self.report.assert_not_called()

    def test_preparation_commit_then_soft_timeout_retains_private_artifact(self):
        job = self.job()
        finish = self.store.finish_pdf_preparation
        def commit_then_timeout(*args):
            self.assertIsNotNone(finish(*args))
            raise SoftTimeLimitExceeded()
        with patch.object(self.store, 'finish_pdf_preparation', side_effect=commit_then_timeout), self.assertRaises(SoftTimeLimitExceeded):
            self.runner.run_job(job.id, job.translation_stats['attempt_id'])
        saved = self.store.get(job.id)
        self.assertEqual(saved.status, self.JobStatus.awaiting_confirmation)
        self.product.validate_pdf_job(saved, self.output)
        self.notify.assert_not_called()

    def test_cancel_before_preparation_commit_rejects_old_writer(self):
        job = self.job()
        finish = self.store.finish_pdf_preparation
        def cancel_then_finish(job_id, attempt, owner, prepared):
            # External cancellation is deliberately outside this old worker's context.
            from sqlalchemy import update
            with self.engine.begin() as connection:
                connection.execute(update(self.JobRecord).where(self.JobRecord.id == job_id).values(
                    status=self.JobStatus.cancelled.value, message='Cancelled externally'))
            return finish(job_id, attempt, owner, prepared)
        with patch.object(self.store, 'finish_pdf_preparation', side_effect=cancel_then_finish):
            self.runner.run_job(job.id, job.translation_stats['attempt_id'])
        saved = self.store.get(job.id)
        self.assertEqual(saved.status, self.JobStatus.cancelled)
        self.assertEqual(saved.message, 'Cancelled externally')
        self.assertEqual(saved.translation_stats['pdf_conversion']['phase'], 'preparing')
        self.assertIsNone(saved.output_path)
        self.notify.assert_not_called()

    def test_soft_timeout_preparation_resumes_same_attempt_without_payment(self):
        job = self.job()
        self.parser.side_effect = SoftTimeLimitExceeded()
        with self.assertRaises(SoftTimeLimitExceeded):
            self.runner.run_job(job.id, job.translation_stats['attempt_id'])
        current = self.store.get(job.id)
        self.assertEqual(current.status, self.JobStatus.running)
        self.assertEqual(current.translation_stats['pdf_conversion']['phase'], 'preparing')
        execution = self.store.get_execution(job.id, job.translation_stats['attempt_id'])
        self.assertEqual(self.store.recover_execution(job.id, job.translation_stats['attempt_id'], execution['owner'],
            stale_before=execution['heartbeat_at'] + 1, now=execution['heartbeat_at'] + 2), 'recovered')
        self.parser.side_effect = self.parse
        self.runner.run_job(job.id, job.translation_stats['attempt_id'])
        self.assertEqual(self.store.get(job.id).status, self.JobStatus.awaiting_confirmation)
        self.assertEqual(self.store.get(job.id).payment_resolution, {})
        self.assertEqual(self.parser.call_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
