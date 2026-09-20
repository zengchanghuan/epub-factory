"""Offline price-change regressions: new amounts, frozen old payments and restart continuity."""
import os
import tempfile
import types
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import app.main as main
from app.models import Job, JobStatus, OutputMode
from test_epub_fixture import minimal_epub_bytes


class RepairPricingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)
        self.stack.enter_context(patch.dict(os.environ, {'SKIP_PAYMENT_CHECK': '0', 'ADMIN_SECRET': 'test-admin',
                                                'ALIPAY_APP_ID': '', 'ALIPAY_SELLER_ID': '',
                                                'ALIPAY_DISABLE_PRECREATE': '0'}))
        self.stack.enter_context(patch.object(main, '_REPAIR_UPLOAD_DIR', Path(self.runtime.name)))
        self.stack.enter_context(patch.object(main, '_repair_jobs', {}))
        self.stack.enter_context(patch.object(main, '_repair_active_jobs', set()))
        self.thread = Mock()
        self.stack.enter_context(patch.object(main, '_threading', types.SimpleNamespace(Thread=self.thread)))
        self.client = self.stack.enter_context(TestClient(main.app))

    def webhook(self, order, amount):
        with patch.object(main, 'verify_alipay_notification', return_value=True):
            return self.client.post('/api/v2/webhooks/alipay', data={
                'out_trade_no': order, 'total_amount': amount, 'trade_status': 'TRADE_SUCCESS',
            })

    def repair_job(self, **values):
        job_id = uuid.uuid4().hex
        main._repair_job_set(job_id, status='pending_payment', filename='fixture.epub', **values)
        return job_id

    def test_translation_minimum_uses_new_standard_floor_and_polish_is_unchanged(self):
        from app.engine.cleaners.llm_polish import calculate_polish_price
        self.assertEqual(main.CONVERSION_PRICE_CNY, '1.99')
        self.assertEqual(main.REPAIR_PRICE_CNY, '0.99')
        self.assertEqual(main._calc_translation_price(10), '3.99')
        self.assertEqual(main.TRANSLATION_PRICE_CNY, '5.99')
        self.assertEqual(calculate_polish_price(200000), 5.99)

    def test_single_conversion_quotes_and_persists_199(self):
        with patch('app.infra.alipay.create_alipay_precreate', return_value='alipay://offline') as create:
            response = self.client.post('/api/v2/jobs',
                files={'file': ('fixture.epub', minimal_epub_bytes(), 'application/epub+zip')},
                data={'enable_translation': 'false'},
                headers={'X-Client-Session': 'pricing-' + uuid.uuid4().hex})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['amount'], '1.99')
        self.assertEqual(create.call_args.kwargs['total_amount'], '1.99')
        self.assertEqual(main.job_store.get(result['job_id']).expected_amount, '1.99')

    def test_old_single_orders_keep_saved_or_legacy_amount(self):
        for stored in ('5.99', ''):
            job_id = uuid.uuid4().hex[:12]
            main.job_store.add(Job(id=job_id, source_filename='fixture.epub', input_path='/tmp/not-read',
                trace_id='offline', output_mode=OutputMode.simplified,
                expected_amount=stored, status=JobStatus.pending_payment))
            with patch.object(main.job_store, 'try_mark_paid', return_value=False) as mark:
                self.assertEqual(self.webhook(job_id, '1.99').text, 'fail')
                mark.assert_not_called()
                self.assertEqual(self.webhook(job_id, '5.99').text, 'success')
                mark.assert_called_once_with(job_id)

    def test_diagnose_reports_new_price(self):
        response = self.client.post('/api/v2/repair/diagnose',
            files={'file': ('fixture.epub', minimal_epub_bytes(), 'application/epub+zip')})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['price_cny'], '0.99')
        self.assertEqual(main._repair_job_get(response.json()['job_id'])['quoted_amount'], '0.99')

    def test_old_batch_keeps_original_aggregate_price(self):
        batch_id = uuid.uuid4().hex[:12]
        for index in range(2):
            main.job_store.add(Job(id=uuid.uuid4().hex[:12], source_filename='fixture.epub',
                input_path='/tmp/not-read', trace_id='offline', output_mode=OutputMode.simplified,
                batch_id=batch_id, batch_index=index, batch_size=2,
                expected_amount='11.98' if index == 0 else '', status=JobStatus.pending_payment))
        with patch.object(main, '_release_batch', return_value=True) as release:
            self.assertEqual(self.webhook(f'batch_{batch_id}', '3.98').text, 'fail')
            release.assert_not_called()
            self.assertEqual(self.webhook(f'batch_{batch_id}', '11.98').text, 'success')
            release.assert_called_once_with(batch_id)

    def test_status_restores_frozen_price_before_payment_after_restart(self):
        cases = [
            ({'quoted_amount': amount}, amount) for amount in ('0.99', '1.99', '5.99')
        ] + [
            ({'quoted_amount': '0.99', 'expected_amount': amount}, amount)
            for amount in ('0.01', '1.99', '5.99')
        ] + [({}, '5.99')]
        for values, expected in cases:
            with self.subTest(values=values):
                job_id = self.repair_job(**values)
                metadata = Path(self.runtime.name) / job_id / 'order.json'
                saved = metadata.read_bytes()
                main._repair_jobs.clear()
                response = self.client.get(f'/api/v2/repair/{job_id}/status')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['price_cny'], expected)
                self.assertEqual(response.json()['status'], 'pending_payment')
                self.assertEqual(metadata.read_bytes(), saved)
        self.thread.assert_not_called()

    def test_repair_new_payment_amount_is_frozen_before_gateway(self):
        job_id = self.repair_job(quoted_amount='0.99')
        def gateway(**kwargs):
            self.assertEqual(main._repair_job_get(job_id)['expected_amount'], '0.99')
            self.assertTrue((Path(self.runtime.name) / job_id / 'order.json').is_file())
            return 'alipay://offline'
        with patch('app.infra.alipay.create_alipay_precreate', side_effect=gateway):
            response = self.client.post(f'/api/v2/repair/{job_id}/pay')
        self.assertEqual(response.json()['price_cny'], '0.99')
        main._repair_jobs.clear()  # Simulate an API process restart, with no network/model calls.
        with patch.object(main, 'REPAIR_PRICE_CNY', '2.99'), patch(
                'app.infra.alipay.create_alipay_precreate', return_value='alipay://offline') as create:
            response = self.client.post(f'/api/v2/repair/{job_id}/pay')
        self.assertEqual(response.json()['price_cny'], '0.99')
        self.assertEqual(create.call_args.kwargs['total_amount'], '0.99')
        self.assertEqual(self.webhook(f'repair_{job_id}', '2.99').text, 'fail')
        self.assertEqual(self.webhook(f'repair_{job_id}', '0.99').text, 'success')
        self.assertEqual(self.thread.call_count, 1)
        main._repair_jobs.clear()
        self.assertEqual(main._repair_job_get(job_id)['status'], 'paid')
        self.assertEqual(self.webhook(f'repair_{job_id}', '0.99').text, 'success')
        self.assertEqual(self.thread.call_count, 1)

    def test_old_repair_amounts_survive_restart_and_reject_new_price(self):
        for amount in ('5.99', '1.99'):
            with self.subTest(amount=amount):
                job_id = self.repair_job(expected_amount=amount)
                main._repair_jobs.clear()
                with patch('app.infra.alipay.create_alipay_precreate', return_value='alipay://offline') as create:
                    self.assertEqual(self.client.post(f'/api/v2/repair/{job_id}/pay').json()['price_cny'], amount)
                self.assertEqual(create.call_args.kwargs['total_amount'], amount)
                self.assertEqual(self.webhook(f'repair_{job_id}', '0.99').text, 'fail')
                self.assertEqual(main._repair_job_get(job_id)['status'], 'pending_payment')
                self.assertEqual(self.webhook(f'repair_{job_id}', amount).text, 'success')

    def test_existing_unpaid_quote_keeps_price_after_restart(self):
        for amount in ('5.99', '1.99'):
            with self.subTest(amount=amount):
                job_id = self.repair_job(quoted_amount=amount)
                main._repair_jobs.clear()
                with patch('app.infra.alipay.create_alipay_precreate', return_value='alipay://offline') as create:
                    response = self.client.post(f'/api/v2/repair/{job_id}/pay')
                self.assertEqual(response.json()['price_cny'], amount)
                self.assertEqual(create.call_args.kwargs['total_amount'], amount)

    def test_legacy_in_memory_repair_has_original_599_amount(self):
        job_id = uuid.uuid4().hex
        main._repair_jobs[job_id] = {'status': 'pending_payment', 'out_trade_no': f'repair_{job_id}'}
        with patch('app.infra.alipay.create_alipay_precreate', return_value='alipay://offline'):
            self.assertEqual(self.client.post(f'/api/v2/repair/{job_id}/pay').json()['price_cny'], '5.99')
        self.assertEqual(self.webhook(f'repair_{job_id}', '1.99').text, 'fail')
        self.assertEqual(self.webhook(f'repair_{job_id}', '5.99').text, 'success')

    def test_admin_test_price_is_also_frozen_and_validated(self):
        job_id = self.repair_job()
        with patch('app.infra.alipay.create_alipay_precreate', return_value='alipay://offline'):
            first = self.client.post(f'/api/v2/repair/{job_id}/pay', data={'admin_key': 'test-admin'})
            second = self.client.post(f'/api/v2/repair/{job_id}/pay')
        self.assertEqual(first.json()['price_cny'], '0.01')
        self.assertEqual(second.json()['price_cny'], '0.01')
        self.assertEqual(self.webhook(f'repair_{job_id}', '1.99').text, 'fail')
        self.assertEqual(self.webhook(f'repair_{job_id}', '0.01').text, 'success')

    def test_recovery_requires_verified_matching_amount(self):
        job_id = self.repair_job(expected_amount='5.99')
        for trade in (None, {'trade_status': 'TRADE_SUCCESS', 'total_amount': '1.99'}):
            with patch('app.infra.alipay.query_verified_trade', return_value=trade):
                response = self.client.post(f'/api/v2/repair/{job_id}/recover')
            self.assertFalse(response.json()['recovered'])
            self.assertEqual(main._repair_job_get(job_id)['status'], 'pending_payment')
        with patch('app.infra.alipay.query_verified_trade', return_value={
                'trade_status': 'TRADE_SUCCESS', 'total_amount': '5.99'}):
            response = self.client.post(f'/api/v2/repair/{job_id}/recover')
        self.assertTrue(response.json()['recovered'])
        main._repair_jobs.clear()
        self.assertEqual(main._repair_job_get(job_id)['status'], 'paid')
        self.assertEqual(self.thread.call_count, 1)

    def test_metadata_path_cannot_escape_upload_directory(self):
        self.assertIsNone(main._repair_job_get('..'))
        self.assertIsNone(main._repair_job_get('../other'))

    def test_paid_repair_resumes_once_on_explicit_recovery_after_process_restart(self):
        job_id = self.repair_job(expected_amount='5.99')
        main._repair_job_set(job_id, status='paid')
        main._repair_jobs.clear()
        for _ in range(2):
            self.assertEqual(self.client.get(f'/api/v2/repair/{job_id}/status').json()['status'], 'paid')
        self.thread.assert_not_called()
        self.client.post(f'/api/v2/repair/{job_id}/recover')
        self.client.post(f'/api/v2/repair/{job_id}/recover')
        self.client.post(f'/api/v2/repair/{job_id}/pay')
        self.assertEqual(self.thread.call_count, 1)

    def test_callback_during_gateway_request_is_not_downgraded(self):
        job_id = self.repair_job(quoted_amount='1.99')
        def gateway(**kwargs):
            # Simulate verified callback work before the gateway reply is received.
            main._repair_job_set(job_id, status='repaired', download_filename='fixture_fixed.epub')
            return 'alipay://offline'
        with patch('app.infra.alipay.create_alipay_precreate', side_effect=gateway):
            response = self.client.post(f'/api/v2/repair/{job_id}/pay')
        self.assertEqual(response.json()['status'], 'repaired')
        main._repair_jobs.clear()
        self.assertEqual(main._repair_job_get(job_id)['status'], 'repaired')

    def test_incomplete_repair_output_is_never_published(self):
        job_id = self.repair_job(expected_amount='1.99')
        directory = Path(self.runtime.name) / job_id
        (directory / 'fixture.epub').write_bytes(minimal_epub_bytes())
        main._repair_job_set(job_id, status='paid')
        def interrupted(source, target):
            Path(target).write_bytes(b'partial zip')
            raise ValueError('offline simulated repair failure')
        with patch('app.engine.epub_repairer.repair', side_effect=interrupted):
            main._do_repair_async(job_id)
        self.assertEqual(main._repair_job_get(job_id)['status'], 'failed')
        self.assertFalse((directory / 'fixture_fixed.epub').exists())
        self.assertFalse((directory / 'fixture_fixed.pending.epub').exists())


if __name__ == '__main__':
    unittest.main()
