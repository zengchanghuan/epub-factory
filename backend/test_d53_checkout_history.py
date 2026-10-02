"""Opt-in historical payment-entry resumption with the existing three books.

Inherits four R1 entitlement/download gates, then proves original merchant IDs
and frozen prices survive re-signing, repeated verified recovery dispatches once,
and completed-book refresh keeps prior artifact bytes. Profile and gateway facts
are boundary stubs; real SQLite/outbox/dispatch orchestration runs, with only the
broker publisher replaced. No models, live payments, workers or new translations.
"""
from __future__ import annotations

import hashlib
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_d37_entitlement_history as history


class HistoricalCheckoutTests(history.HistoricalEntitlementTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.real_enqueue = staticmethod(cls.main._enqueue_conversion)

    def setUp(self):
        super().setUp()
        self.checkout_query = self.patches.enter_context(patch('app.infra.alipay.query_checkout_trade', return_value=None))
        self.precreate = self.patches.enter_context(patch('app.infra.alipay.create_alipay_precreate',
                                                       side_effect=AssertionError('Resume must not precreate a new order')))
        self.publish = self.patches.enter_context(patch('app.infra.job_dispatch_publisher.publish_conversion'))
        self.patches.enter_context(patch.object(self.main, '_use_celery', return_value=True))

    def resume(self, job, headers):
        return self.client.post(f'/api/v2/jobs/{job.id}/continue-payment', headers=headers)

    def pending_payment_book(self, book):
        from app.models import JobStatus
        job, headers = self.create_waiting(book)
        response = self.client.post(f'/api/v2/jobs/{job.id}/confirm-profile', headers=headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        job = self.store.get(job.id)
        self.assertEqual(job.status, JobStatus.pending_payment)
        self.assertEqual(response.json()['amount'], job.expected_amount)
        return job, headers

    def test_historical_unpaid_checkout_reuses_same_order_quote_and_source_after_refresh(self):
        from app.storage_db import PersistentJobStore
        for count, book in enumerate(history.BOOKS, start=1):
            with self.subTest(book=book['key']):
                job, headers = self.pending_payment_book(book)
                self.payment.reset_mock()
                self.checkout_query.reset_mock()
                self.checkout_query.return_value = {'out_trade_no': job.id, 'trade_status': 'NOT_CREATED'}
                before_quote = job.translation_stats['translation_pricing']
                with patch.object(self.main, '_calc_translation_price', side_effect=AssertionError('No repricing old orders')):
                    for _ in range(3):
                        reloaded = PersistentJobStore(engine=self.store._engine)
                        with patch.object(self.main, 'job_store', reloaded):
                            response = self.resume(job, headers)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertTrue(response.json()['checkout_available'])
                        self.assertEqual(response.json()['amount'], job.expected_amount)
                        self.assertEqual(response.json()['pricing'], before_quote)
                        self.assertEqual(self.payment.call_args.kwargs['out_trade_no'], job.id)
                        self.assertEqual(self.payment.call_args.kwargs['total_amount'], job.expected_amount)
                        self.assertEqual(reloaded.get(job.id), job)
                self.assertEqual(self.payment.call_count, 3)
                self.assertEqual(self.checkout_query.call_count, 3)
                self.assertEqual(len(self.store.list_jobs()), count)
                self.assertEqual(self.store.list_dispatches(job.id), [])
                self.assertEqual(history.sha256(self.history_uploads / book['input']), book['input_sha256'])
        self.precreate.assert_not_called()
        self.enqueue.assert_not_called()
        self.publish.assert_not_called()

    def test_historical_verified_recovery_survives_reload_without_duplicate_job_or_dispatch(self):
        from app.models import JobStatus
        from app.storage_db import PersistentJobStore
        for count, book in enumerate(history.BOOKS, start=1):
            with self.subTest(book=book['key']):
                job, headers = self.pending_payment_book(book)
                self.payment.reset_mock()
                self.publish.reset_mock()
                self.checkout_query.reset_mock()
                self.checkout_query.return_value = {'out_trade_no': job.id, 'trade_status': 'TRADE_SUCCESS',
                                                    'total_amount': job.expected_amount, 'trade_no': 'offline-verified-receipt'}
                # Use real durable dispatch; only its broker transport is fake.
                with patch.object(self.main, '_enqueue_conversion', self.real_enqueue):
                    for _ in range(3):
                        reloaded = PersistentJobStore(engine=self.store._engine)
                        with patch.object(self.main, 'job_store', reloaded):
                            response = self.resume(job, headers)
                            detail = self.client.get(f'/api/v2/jobs/{job.id}', headers=headers)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertFalse(response.json()['checkout_available'])
                        self.assertIsNone(response.json()['pay_url'])
                        self.assertEqual(detail.status_code, 200, detail.text)
                        self.assertEqual(detail.json()['status'], 'queued')
                        self.assertEqual(detail.json()['amount'], job.expected_amount)
                paid = reloaded.get(job.id)
                self.assertEqual(paid.status, JobStatus.pending)
                self.assertEqual(paid.payment_entitlement['state'], 'paid')
                self.assertEqual(paid.payment_entitlement['amount'], job.expected_amount)
                self.assertEqual(paid.translation_stats['translation_pricing'], job.translation_stats['translation_pricing'])
                self.assertEqual(len(self.store.list_jobs()), count)
                intents = reloaded.list_dispatches(job.id)
                self.assertEqual(len(intents), 1)
                self.assertEqual(intents[0]['status'], 'sent')
                self.publish.assert_called_once_with(job.id, paid.translation_stats['attempt_id'])
                self.checkout_query.assert_called_once_with(job.id)
                self.payment.assert_not_called()
        self.precreate.assert_not_called()

    def test_historical_conversion_refresh_keeps_saved_qr_and_never_switches_product(self):
        from app.storage_db import PersistentJobStore
        self.precreate.side_effect = None
        for book in history.BOOKS:
            with self.subTest(book=book['key']):
                self.precreate.reset_mock()
                self.payment.reset_mock()
                original_qr = 'https://offline.invalid/qr-' + book['key']
                self.precreate.return_value = original_qr
                with (self.history_uploads / book['input']).open('rb') as source:
                    created = self.client.post('/api/v2/jobs', files={
                        'file': (book['input'], source, 'application/epub+zip'),
                    }, data={'output_mode': 'simplified', 'enable_translation': 'false'})
                self.assertEqual(created.status_code, 200, created.text)
                payload = created.json()
                job = self.store.get(payload['job_id'])
                headers = {'X-Job-Token': payload['access_token']}
                self.assertEqual(payload['qr_code'], original_qr)
                self.assertEqual(job.translation_stats['payment_checkout']['channel'], 'qr')
                self.checkout_query.return_value = {'out_trade_no': job.id, 'trade_status': 'WAIT_BUYER_PAY',
                                                    'total_amount': job.expected_amount}
                for _ in range(3):
                    reloaded = PersistentJobStore(engine=self.store._engine)
                    with patch.object(self.main, 'job_store', reloaded):
                        response = self.resume(job, headers)
                        detail = self.client.get(f'/api/v2/jobs/{job.id}', headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()['qr_code'], original_qr)
                    self.assertIsNone(response.json()['pay_url'])
                    self.assertTrue(response.json()['checkout_available'])
                    self.assertEqual(response.json()['amount'], job.expected_amount)
                    self.assertEqual(detail.status_code, 200, detail.text)
                    self.assertNotIn('payment_checkout', detail.json().get('translation_stats') or {})
                    self.assertNotIn(original_qr, detail.text)
                    self.assertEqual(reloaded.get(job.id), job)
                self.precreate.assert_called_once()
                self.payment.assert_not_called()
                self.assertEqual(self.store.list_dispatches(job.id), [])
        self.publish.assert_not_called()
        self.enqueue.assert_not_called()

    def test_completed_historical_refresh_never_opens_gateway_and_keeps_download_sha(self):
        from app.models import Job, JobStatus, OutputMode
        from app.storage_db import PersistentJobStore
        for book in history.BOOKS:
            with self.subTest(book=book['key']):
                output = self.outputs / book['output']
                shutil.copyfile(self.history_outputs / book['output'], output)
                job = Job(id=book['key'], trace_id='d53-history', source_filename=book['input'],
                          input_path=str(self.history_uploads / book['input']), output_path=str(output),
                          output_mode=OutputMode.simplified, status=JobStatus.success, expected_amount='5.99',
                          access_token='historical-token-' + book['key'],
                          token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
                self.store.add(job)
                headers = {'X-Job-Token': job.access_token}
                for _ in range(2):
                    reloaded = PersistentJobStore(engine=self.store._engine)
                    with patch.object(self.main, 'job_store', reloaded):
                        response = self.resume(job, headers)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()['status'], 'completed')
                        self.assertFalse(response.json()['checkout_available'])
                        self.assertIsNone(response.json()['pay_url'])
                        self.assertEqual(response.json()['amount'], '5.99')
                        self.assertIsNone(response.json()['pricing'])  # Legacy order: no retroactive quote.
                        detail = self.client.get(f'/api/v2/jobs/{job.id}', headers=headers)
                        self.assertEqual(detail.status_code, 200, detail.text)
                        download = self.client.get(detail.json()['download_url'])
                        self.assertEqual(download.status_code, 200)
                        self.assertEqual(hashlib.sha256(download.content).hexdigest(), book['output_sha256'])
                    self.assertEqual(reloaded.get(job.id), self.store.get(job.id))
                self.assertEqual(history.sha256(output), book['output_sha256'])
        self.payment.assert_not_called()
        self.checkout_query.assert_not_called()
        self.publish.assert_not_called()
        self.enqueue.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
