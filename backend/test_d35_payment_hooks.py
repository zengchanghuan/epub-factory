"""Offline payment verification -> merchant receipt tests; never run a book."""
from contextlib import ExitStack
from pathlib import Path
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import main
from app.domain import payment_email_service as mail
from app.domain.payment_email_repository import PaymentEmailRepository
from app.models import Job, JobStatus, OutputMode
from app.storage import JobStore


class PaymentHookTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {
            'ALIPAY_APP_ID': 'expected-app', 'ALIPAY_SELLER_ID': 'expected-seller',
            'OWNER_PAYMENT_EMAIL_ENABLED': '1', 'OWNER_PAYMENT_EMAIL_TO': '249998620@qq.com',
            'NOTIFY_EMAIL_ENABLED': '0', 'SMTP_HOST': 'smtp.example.com', 'SMTP_USER': 'sender@example.com',
            'SMTP_PASSWORD': 'offline-only', 'SMTP_FROM': 'sender@example.com',
            'SMTP_PORT': '465', 'SMTP_SECURITY': 'ssl', 'SITE_BASE_URL': 'https://fixepub.com',
        }))
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')))
        self.store = JobStore()
        self.stack.enter_context(patch.object(main, 'job_store', self.store))
        self.stack.enter_context(patch.object(mail, 'job_store', self.store))
        self.stack.enter_context(patch.object(main, '_REPAIR_UPLOAD_DIR', Path(self.temp)))
        self.stack.enter_context(patch.object(main, '_repair_jobs', {}))
        self.events = self.stack.enter_context(patch.object(main, 'record_event'))
        self.verify = self.stack.enter_context(patch.object(main, 'verify_alipay_notification', return_value=True))
        self.stack.enter_context(patch.object(main, '_use_celery', return_value=True))
        self.enqueue = self.stack.enter_context(patch('app.tasks.job_pipeline.run_conversion.delay'))
        self.batch_enqueue = self.stack.enter_context(patch.object(main, '_enqueue_batch'))
        self.repair_run = self.stack.enter_context(patch.object(main, '_ensure_repair_running'))
        self.wake = self.stack.enter_context(patch('app.domain.payment_email_worker.payment_email_worker.wake'))
        self.send = self.stack.enter_context(patch.object(mail.completion_mail, '_send_email'))
        self.repo = PaymentEmailRepository(self.store)
        app = FastAPI()
        app.add_api_route('/webhook', main.alipay_webhook, methods=['POST'])
        app.add_api_route('/jobs/{job_id}/recover', main.recover_job_payment, methods=['POST'])
        app.add_api_route('/batches/{batch_id}/recover', main.recover_batch_payment_v2, methods=['POST'])
        app.add_api_route('/repair/{job_id}/recover', main.repair_recover_payment, methods=['POST'])
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def job(self, key='paid-job', **extra):
        values = dict(id=key, source_filename='private.epub', trace_id='offline', input_path='/unused',
            output_mode=OutputMode.simplified, expected_amount='1.99', status=JobStatus.pending_payment,
            access_token='private-owner-token')
        values.update(extra)
        job = Job(**values)
        self.store.add(job)
        return job

    def callback(self, order='paid-job', **extra):
        values = dict(out_trade_no=order, total_amount='1.99', trade_status='TRADE_SUCCESS',
                      app_id='expected-app', seller_id='expected-seller')
        values.update(extra)
        return self.client.post('/webhook', data=values)

    def test_receipt_sends_while_book_is_still_pending_and_duplicates_do_not_resend(self):
        self.job()
        self.assertEqual(self.callback().text, 'success')
        self.assertEqual(self.store.get('paid-job').status, JobStatus.pending)
        self.send.assert_not_called()  # HTTP path only persists and wakes the dispatcher.
        self.wake.assert_called_once()
        self.assertEqual(mail.dispatch_pending_payment_emails()['sent'], 1)
        self.assertEqual(self.send.call_args.args[0], '249998620@qq.com')
        body = self.send.call_args.args[2]
        self.assertIn('1.99', body)
        self.assertIn('paid-job', body)
        self.assertIn('orders-admin.html', body)
        self.assertNotIn('private-owner-token', body)
        self.assertNotIn('private.epub', body)
        self.callback()
        self.callback(trade_status='TRADE_FINISHED')
        mail.dispatch_pending_payment_emails()
        self.send.assert_called_once()
        self.enqueue.assert_called_once()

    def test_unverified_wrong_account_wrong_amount_unpaid_and_unknown_do_not_notify(self):
        self.job()
        self.verify.return_value = False
        self.assertEqual(self.callback().text, 'fail')
        self.verify.return_value = True
        for values in ({'app_id': 'wrong'}, {'seller_id': 'wrong'}, {'total_amount': '0.01'},
                       {'trade_status': 'WAIT_BUYER_PAY'}, {'trade_status': 'TRADE_CLOSED'}):
            self.callback(**values)
        self.callback(order='unknown')
        self.assertIsNone(self.repo.get('paid-job'))
        self.wake.assert_not_called()
        self.enqueue.assert_not_called()
        self.assertEqual(self.store.get('paid-job').status, JobStatus.pending_payment)

    def test_batch_is_one_receipt_for_the_total(self):
        for index in range(2):
            self.job('batch-job-' + str(index), batch_id='batch-id', batch_index=index, batch_size=2,
                     expected_amount='3.98' if index == 0 else '')
        self.assertEqual(self.callback(order='batch_batch-id', total_amount='3.98').text, 'success')
        self.callback(order='batch_batch-id', total_amount='3.98')
        self.assertEqual(mail.dispatch_pending_payment_emails()['sent'], 1)
        body = self.send.call_args.args[2]
        self.assertIn('3.98', body)
        self.assertIn('2 本', body)
        self.batch_enqueue.assert_called_once_with('batch-id', None)

    def test_server_marked_test_order_still_runs_without_merchant_email(self):
        self.job(is_test_order=True, expected_amount='0.02')
        self.assertEqual(self.callback(total_amount='0.02').text, 'success')
        self.assertEqual(self.store.get('paid-job').status, JobStatus.pending)
        self.assertIsNone(self.repo.get('paid-job'))
        self.wake.assert_not_called()

    def test_historical_single_order_callbacks_do_not_create_receipts(self):
        for status in (JobStatus.pending, JobStatus.running, JobStatus.success,
                       JobStatus.failed, JobStatus.cancelled):
            with self.subTest(status=status):
                key = 'historical-' + status.value
                self.job(key, status=status)
                for trade_status in ('TRADE_SUCCESS', 'TRADE_FINISHED'):
                    self.assertEqual(self.callback(order=key, trade_status=trade_status).text, 'success')
                self.assertIsNone(self.repo.get(key))
                self.assertEqual(self.store.get(key).status, status)
        self.wake.assert_not_called()
        self.enqueue.assert_not_called()
        self.assertEqual(mail.dispatch_pending_payment_emails()['sent'], 0)
        self.send.assert_not_called()

    def test_historical_batch_callbacks_do_not_create_receipts(self):
        for index in range(2):
            self.job('historical-batch-' + str(index), status=JobStatus.success,
                     batch_id='old-batch', batch_index=index, batch_size=2,
                     expected_amount='3.98' if index == 0 else '')
        for trade_status in ('TRADE_SUCCESS', 'TRADE_FINISHED'):
            self.assertEqual(self.callback(order='batch_old-batch', total_amount='3.98',
                                           trade_status=trade_status).text, 'success')
        self.assertIsNone(self.repo.get('batch_old-batch'))
        self.wake.assert_not_called()
        self.batch_enqueue.assert_not_called()
        self.assertEqual(mail.dispatch_pending_payment_emails()['sent'], 0)
        self.send.assert_not_called()

    def test_historical_repair_callbacks_do_not_create_receipts(self):
        for index, status in enumerate(('paid', 'repaired', 'failed')):
            with self.subTest(status=status):
                key = format(index + 1, '032x')
                main._repair_job_set(key, status=status, expected_amount='1.99')
                for trade_status in ('TRADE_SUCCESS', 'TRADE_FINISHED'):
                    self.assertEqual(self.callback(order='repair_' + key,
                                                   trade_status=trade_status).text, 'success')
                self.assertIsNone(self.repo.get('repair_' + key))
                self.assertEqual(main._repair_job_get(key)['status'], status)
        self.wake.assert_not_called()
        self.repair_run.assert_not_called()
        self.assertEqual(mail.dispatch_pending_payment_emails()['sent'], 0)
        self.send.assert_not_called()

    def test_repair_callback_and_recover_share_one_receipt(self):
        key = 'a' * 32
        main._repair_job_set(key, status='pending_payment', expected_amount='1.99')
        with patch('app.infra.alipay.query_verified_trade', return_value={
                'out_trade_no': 'repair_' + key, 'trade_status': 'TRADE_SUCCESS', 'total_amount': '1.99'}):
            result = self.client.post('/repair/' + key + '/recover')
        self.assertTrue(result.json()['recovered'])
        self.assertEqual(self.callback(order='repair_' + key).text, 'success')
        self.assertEqual(mail.dispatch_pending_payment_emails()['sent'], 1)
        self.assertIn('EPUB 修复', self.send.call_args.args[2])
        self.send.assert_called_once()

    def test_repair_test_order_is_suppressed(self):
        key = 'b' * 32
        main._repair_job_set(key, status='pending_payment', expected_amount='0.01', is_test_order=True)
        self.callback(order='repair_' + key, total_amount='0.01')
        self.assertEqual(main._repair_job_get(key)['status'], 'paid')
        self.assertIsNone(self.repo.get('repair_' + key))

    def test_recover_requires_verified_amount_and_notifies_once(self):
        self.job(enable_translation=True, expected_amount='5.99')
        with patch('app.infra.alipay.query_verified_trade') as query:
            for trade in (None, {'trade_status': 'TRADE_SUCCESS', 'total_amount': '1.99'}):
                query.return_value = trade
                result = self.client.post('/jobs/paid-job/recover', headers={'X-Job-Token': 'private-owner-token'})
                self.assertFalse(result.json()['recovered'])
                self.assertIsNone(self.repo.get('paid-job'))
            query.return_value = {'trade_status': 'TRADE_FINISHED', 'total_amount': '5.99'}
            result = self.client.post('/jobs/paid-job/recover', headers={'X-Job-Token': 'private-owner-token'})
            self.assertTrue(result.json()['recovered'])
        self.assertEqual(mail.dispatch_pending_payment_emails()['sent'], 1)
        self.assertIn('AI 翻译', self.send.call_args.args[2])
        self.callback(total_amount='5.99')
        mail.dispatch_pending_payment_emails()
        self.send.assert_called_once()

    def test_batch_recover_rejects_wrong_total_before_notification_or_release(self):
        for index in range(2):
            self.job('batch-job-' + str(index), batch_id='batch-id', batch_index=index, batch_size=2,
                     expected_amount='3.98' if index == 0 else '')
        with patch('app.infra.alipay.query_verified_trade') as query:
            query.return_value = {'trade_status': 'TRADE_SUCCESS', 'total_amount': '1.99'}
            result = self.client.post('/batches/batch-id/recover', headers={'X-Job-Token': 'private-owner-token'})
            self.assertFalse(result.json()['recovered'])
            self.assertIsNone(self.repo.get('batch_batch-id'))
            self.batch_enqueue.assert_not_called()
            query.return_value = {'trade_status': 'TRADE_SUCCESS', 'total_amount': '3.98'}
            result = self.client.post('/batches/batch-id/recover', headers={'X-Job-Token': 'private-owner-token'})
            self.assertTrue(result.json()['recovered'])
        self.assertEqual(mail.dispatch_pending_payment_emails()['sent'], 1)

    def test_smtp_failure_and_queue_exception_do_not_stop_paid_processing(self):
        self.job()
        self.send.side_effect = TimeoutError('private-provider-response')
        self.callback()
        self.assertEqual(mail.dispatch_pending_payment_emails()['retried'], 1)
        self.assertEqual(self.store.get('paid-job').status, JobStatus.pending)
        self.job('queue-error')
        with patch.object(mail, 'queue_paid_order_email', side_effect=RuntimeError('private-error')):
            self.assertEqual(self.callback(order='queue-error').text, 'success')
        self.assertEqual(self.store.get('queue-error').status, JobStatus.pending)


if __name__ == '__main__':
    unittest.main()
