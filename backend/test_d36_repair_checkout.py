"""Repair checkout regressions with genuine EPUB fixtures and a forbidden network."""
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import uuid
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import main
from app.domain.repair_payment_worker import RepairPaymentWorker
from test_epub_fixture import minimal_epub_bytes


def fixable_epub():
    target = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(minimal_epub_bytes())) as source, zipfile.ZipFile(target, 'w') as out:
        for item in source.infolist():
            out.writestr(item.filename, source.read(item), compress_type=zipfile.ZIP_DEFLATED)
    return target.getvalue()


class RepairCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {'SKIP_PAYMENT_CHECK': '0', 'ADMIN_SECRET': 'offline-admin',
            'ALIPAY_APP_ID': 'app', 'ALIPAY_SELLER_ID': 'seller', 'ALIPAY_DISABLE_PRECREATE': '0'}))
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')))
        self.stack.enter_context(patch.object(main, '_REPAIR_UPLOAD_DIR', Path(self.temp)))
        self.stack.enter_context(patch.object(main, '_repair_jobs', {}))
        self.stack.enter_context(patch.object(main, '_repair_active_jobs', set()))
        self.stack.enter_context(patch.object(main, '_repair_last_gateway_check', 0))
        self.clock = self.stack.enter_context(patch.object(main, '_repair_now', return_value=1_000_000))
        self.ready = self.stack.enter_context(patch.object(main, '_repair_gateway_available', return_value=True))
        self.repair = self.stack.enter_context(patch.object(main, '_ensure_repair_running'))
        self.record = self.stack.enter_context(patch.object(main, 'record_event', return_value=True))
        self.receipt = self.stack.enter_context(patch('app.domain.payment_email_service.queue_paid_order_email'))
        self.gateway = self.stack.enter_context(patch('app.infra.alipay.create_alipay_precreate', return_value='alipay://offline'))
        self.query = self.stack.enter_context(patch('app.infra.alipay.query_verified_trade', return_value=None))
        self.stack.enter_context(patch.object(main, 'verify_alipay_notification', return_value=True))
        app = FastAPI()
        for suffix, endpoint, method in [('/diagnose', main.repair_diagnose, 'POST'),
            ('/{job_id}/pay', main.repair_pay, 'POST'), ('/{job_id}/status', main.repair_status, 'GET'),
            ('/{job_id}/recover', main.repair_recover_payment, 'POST'),
            ('/{job_id}/checkout-events', main.repair_checkout_event, 'POST'),
            ('/{job_id}/download', main.repair_download, 'GET')]:
            app.add_api_route('/api/v2/repair' + suffix, endpoint, methods=[method])
        app.add_api_route('/webhook', main.alipay_webhook, methods=['POST'])
        self.client = self.stack.enter_context(TestClient(app))

    def upload(self, content=None):
        response = self.client.post('/api/v2/repair/diagnose', files={
            'file': ('fixture.epub', content if content is not None else fixable_epub(), 'application/epub+zip')})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def path(self, job, endpoint):
        return f'/api/v2/repair/{job}/{endpoint}'

    def pay(self, job, **kwargs):
        return self.client.post(self.path(job, 'pay'), **kwargs)

    def paid_trade(self, job, amount='2.99', **extra):
        return dict(out_trade_no='repair_' + job, total_amount=amount, trade_status='TRADE_SUCCESS', **extra)

    def recover(self, job):
        return self.client.post(self.path(job, 'recover')).json()

    def advance(self, seconds=4000):
        self.clock.return_value += seconds

    def callback(self, job, amount='2.99', **extra):
        data = dict(out_trade_no='repair_' + job, total_amount=amount, trade_status='TRADE_SUCCESS', app_id='app', seller_id='seller')
        data.update(extra)
        return self.client.post('/webhook', data=data)

    def test_clean_epub_cannot_pay_after_refresh_or_direct_request(self):
        response = self.upload(minimal_epub_bytes())
        self.assertFalse(response['can_pay'])
        job = response['job_id']
        main._repair_jobs.clear()
        self.assertFalse(self.client.get(self.path(job, 'status')).json()['can_pay'])
        self.assertEqual(self.pay(job).status_code, 409)
        self.gateway.assert_not_called()

    def test_bad_zip_and_arbitrary_zip_cannot_be_charged(self):
        arbitrary = io.BytesIO()
        with zipfile.ZipFile(arbitrary, 'w') as archive:
            archive.writestr('not-a-book.txt', 'fixture')
        for content in (b'not a zip', arbitrary.getvalue()):
            with self.subTest(size=len(content)):
                info = self.upload(content)
                self.assertFalse(info['can_pay'])
                main._repair_jobs.clear()
                self.assertEqual(self.pay(info['job_id']).status_code, 409)
        self.gateway.assert_not_called()

    def test_valid_repairable_epub_persists_eligibility_and_checkout_amount(self):
        info = self.upload()
        job = info['job_id']
        self.assertTrue(info['can_pay'])
        saved = json.loads((Path(self.temp) / job / 'order.json').read_text())
        self.assertEqual(saved['report']['fixable_count'], 1)
        self.assertTrue(saved['can_pay'])
        main._repair_jobs.clear()
        self.assertTrue(self.client.get(self.path(job, 'status')).json()['can_pay'])
        self.assertEqual(self.pay(job).json()['price_cny'], '2.99')
        saved = main._repair_job_get(job)
        self.assertEqual(saved['checkout_started_at'], self.clock.return_value)
        self.assertEqual(saved['expected_amount'], '2.99')
        self.record.assert_any_call(main.job_store, 'repair_' + job, 'payment_clicked', 'pay_request')
        self.query.assert_not_called()

    def test_source_corrupted_after_diagnosis_blocks_new_checkout(self):
        job = self.upload()['job_id']
        (Path(self.temp) / job / 'fixture.epub').write_bytes(b'broken')
        self.assertEqual(self.pay(job).status_code, 409)
        self.gateway.assert_not_called()

    def test_legacy_requalification_preserves_quote_and_status_reads_do_not_write(self):
        job = uuid.uuid4().hex
        main._repair_job_set(job, status='pending_payment', filename='fixture.epub', quoted_amount='5.99')
        path = Path(self.temp) / job
        (path / 'fixture.epub').write_bytes(fixable_epub())
        before = (path / 'order.json').read_bytes()
        status = self.client.get(self.path(job, 'status')).json()
        self.assertTrue(status['can_pay'])
        self.assertEqual((path / 'order.json').read_bytes(), before)
        self.assertEqual(self.pay(job).json()['price_cny'], '5.99')

    def test_paid_order_recovery_is_not_blocked_by_current_source_eligibility(self):
        job = self.upload(minimal_epub_bytes())['job_id']
        main._repair_job_set(job, expected_amount='2.99', out_trade_no='repair_' + job)
        self.query.return_value = self.paid_trade(job)
        self.assertTrue(self.recover(job)['recovered'])
        self.repair.assert_called_with(job)
        self.assertEqual(main._repair_job_get(job)['status'], 'paid')

    def test_wrong_amount_identity_or_unverified_query_cannot_confirm_payment(self):
        job = self.upload()['job_id']
        self.pay(job)
        for trade in (None, dict(out_trade_no='other', total_amount='2.99', trade_status='TRADE_SUCCESS'), self.paid_trade(job, '5.99')):
            self.advance()
            self.query.return_value = trade
            result = self.recover(job)
            self.assertEqual(result['payment_check'], 'unavailable')
            self.assertFalse(result['recovered'])
            self.assertEqual(result['status'], 'pending_payment')
        self.repair.assert_not_called()
        self.receipt.assert_not_called()

    def test_query_exception_does_not_mean_unpaid_and_persists_backoff(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.advance(11)
        self.query.side_effect = RuntimeError('provider unavailable')
        result = self.recover(job)
        self.assertEqual(result['payment_check'], 'unavailable')
        self.assertGreater(result['retry_after_seconds'], 0)
        main._repair_jobs.clear()
        self.assertEqual(self.recover(job)['payment_check'], 'throttled')
        self.query.assert_called_once()
        self.assertEqual(main._repair_job_get(job)['status'], 'pending_payment')

    def test_confirmed_unpaid_has_distinct_result(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.advance(11)
        self.query.return_value = dict(out_trade_no='repair_' + job, trade_status='WAIT_BUYER_PAY')
        self.assertEqual(self.recover(job)['payment_check'], 'pending')
        self.receipt.assert_not_called()

    def test_closed_browser_and_api_restart_are_recovered_by_background_tick(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.advance(11)
        main._repair_jobs.clear()
        self.query.return_value = self.paid_trade(job)
        main._repair_payment_tick()
        self.assertEqual(main._repair_job_get(job)['status'], 'paid')
        self.repair.assert_called_once_with(job)
        self.receipt.assert_called_once()
        self.assertEqual(self.callback(job).text, 'success')
        main._repair_payment_tick()
        self.query.assert_called_once()
        self.receipt.assert_called_once()

    def test_background_does_not_query_historical_ids_or_old_frozen_quotes(self):
        job = self.upload()['job_id']
        main._repair_job_set(job, out_trade_no='repair_' + job, expected_amount='5.99')
        main._repair_payment_tick()
        self.query.assert_not_called()
        # An explicit owner-page recovery can still rescue the historical payment.
        self.query.return_value = self.paid_trade(job, '5.99')
        self.assertTrue(self.recover(job)['recovered'])

    def test_background_window_exhaustion_preserves_order_for_manual_check(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.advance(main._REPAIR_CHECK_WINDOW + 1)
        main._repair_payment_tick()
        self.query.assert_not_called()
        self.assertEqual(main._repair_job_get(job)['status'], 'pending_payment')
        self.assertEqual(main._repair_job_get(job)['payment_check'], 'manual_review_required')
        self.query.return_value = self.paid_trade(job)
        self.assertTrue(self.recover(job)['recovered'])

    def test_repeat_pay_cannot_reset_recovery_budget_or_change_frozen_amount(self):
        job = self.upload()['job_id']
        self.pay(job, data={'admin_key': 'offline-admin'})
        self.advance(11)
        self.recover(job)
        self.advance(1)
        before = dict(main._repair_job_get(job))
        with patch.object(main, 'REPAIR_PRICE_CNY', '9.99'):
            response = self.pay(job)
        self.assertEqual(response.json()['price_cny'], '0.01')
        self.assertTrue(main._repair_job_get(job)['is_test_order'])
        self.assertEqual(main._repair_job_get(job)['next_payment_check_at'], before['next_payment_check_at'])
        self.assertEqual(self.recover(job)['payment_check'], 'throttled')
        self.query.assert_called_once()

    def test_webhook_while_query_in_flight_only_confirms_once(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.advance(11)
        def query(_number):
            self.assertEqual(self.callback(job).text, 'success')
            return self.paid_trade(job)
        self.query.side_effect = query
        self.assertFalse(self.recover(job)['recovered'])
        self.receipt.assert_called_once()
        self.assertEqual(main._repair_job_get(job)['status'], 'paid')

    def test_simultaneous_recovery_is_throttled_without_second_gateway_request(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.advance(11)
        nested = []
        def query(_number):
            nested.append(main._repair_check_payment(job))
            return self.paid_trade(job)
        self.query.side_effect = query
        self.assertTrue(self.recover(job)['recovered'])
        self.assertEqual(nested[0]['payment_check'], 'throttled')
        self.query.assert_called_once()

    def test_browser_events_cannot_forge_payment_and_quote_is_not_diagnose(self):
        job = self.upload()['job_id']
        self.record.assert_not_called()
        for event in ('quote_shown', 'payment_clicked'):
            self.assertEqual(self.client.post(self.path(job, 'checkout-events'), json={'event': event}).status_code, 200)
        self.assertEqual(self.client.post(self.path(job, 'checkout-events'), json={'event': 'payment_succeeded'}).status_code, 422)
        self.assertEqual(main._repair_job_get(job)['status'], 'pending_payment')
        self.assertFalse(main._repair_job_get(job).get('checkout_started_at'))

    def test_gateway_errors_are_sanitized(self):
        job = self.upload()['job_id']
        self.gateway.side_effect = RuntimeError('secret-provider-payload')
        with patch('app.infra.alipay.create_alipay_page_pay', side_effect=ValueError('secret-provider-payload')):
            response = self.pay(job)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('secret-provider-payload', response.text)

    def test_local_repair_and_download_complete_after_verified_payment(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.advance(11)
        self.query.return_value = self.paid_trade(job)
        with patch.object(main, '_queue_repair_completion_email'):
            self.repair.side_effect = lambda key: main._do_repair_async(key)
            result = self.recover(job)
        self.assertTrue(result['recovered'])
        self.assertEqual(result['status'], 'repaired')
        response = self.client.get(self.path(job, 'download'))
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist()[0], 'mimetype')
            self.assertEqual(archive.getinfo('mimetype').compress_type, zipfile.ZIP_STORED)
            self.assertIsNone(archive.testzip())

    def test_nonfatal_warning_does_not_block_fixable_book(self):
        from app.engine.epub_repairer import DiagnoseReport, RepairIssue
        report = DiagnoseReport(total_issues=2, fixable_count=1, unfixable_count=1,
            issues=[RepairIssue('PKG-006', 'error', 'mimetype compressed'),
                    RepairIssue('OPTIONAL', 'warning', 'optional resource missing', fixable=False)])
        with patch('app.engine.epub_repairer.diagnose', return_value=report):
            job = self.upload()
            self.assertTrue(job['can_pay'])
            self.assertEqual(self.pay(job['job_id']).status_code, 200)

    def test_readable_book_with_missing_local_image_can_still_fix_format(self):
        target = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(fixable_epub())) as source, zipfile.ZipFile(target, 'w') as out:
            for item in source.infolist():
                raw = source.read(item)
                if item.filename.endswith('chapter.xhtml'):
                    raw = raw.replace(b'</body>', b'<img src="missing.png" alt="missing"/></body>')
                if item.filename.endswith('package.opf'):
                    raw = raw.replace(b'</manifest>', b'<item id="pic" href="missing.png" media-type="image/png"/></manifest>')
                out.writestr(item.filename, raw, compress_type=zipfile.ZIP_DEFLATED)
        info = self.upload(target.getvalue())
        self.assertTrue(info['can_pay'])
        self.assertTrue(info['report']['source_warnings'])
        self.assertEqual(self.pay(info['job_id']).status_code, 200)

    def test_unavailable_gateway_disables_background_reads_without_consuming_attempts(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.advance(11)
        before = (Path(self.temp) / job / 'order.json').read_bytes()
        self.ready.return_value = False
        for _ in range(3):
            main._repair_payment_tick()
        self.assertEqual((Path(self.temp) / job / 'order.json').read_bytes(), before)
        self.query.assert_not_called()
        self.assertEqual(main._repair_check_payment(job, background=True)['payment_check'], 'unavailable')
        self.query.assert_not_called()
        main._repair_job_set(job, status='paid')
        main._repair_payment_tick()
        self.repair.assert_called_once_with(job)

    def test_unknown_query_finishing_after_callback_cannot_overwrite_verified_state(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.advance(11)
        def query(_number):
            self.callback(job)
            return None
        self.query.side_effect = query
        result = self.recover(job)
        self.assertEqual(result['status'], 'paid')
        self.assertEqual(result['payment_check'], 'verified_paid')
        self.assertEqual(main._repair_job_get(job)['payment_check'], 'verified_paid')

    def test_per_process_gateway_budget_limits_distinct_manual_orders(self):
        jobs = [self.upload()['job_id'] for _ in range(2)]
        for job in jobs:
            self.pay(job)
        self.advance(11)
        self.recover(jobs[0])
        self.assertEqual(self.recover(jobs[1])['payment_check'], 'throttled')
        self.query.assert_called_once()
        self.advance(2)
        self.recover(jobs[1])
        self.assertEqual(self.query.call_count, 2)

    def test_receipt_persistence_failure_does_not_stop_repair_and_recovers_after_restart(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.receipt.return_value = False
        with patch('app.domain.payment_email_repository.PaymentEmailRepository.get', return_value=None):
            self.assertEqual(self.callback(job).text, 'success')
        self.assertEqual(main._repair_job_get(job)['status'], 'paid')
        self.assertTrue(main._repair_job_get(job)['payment_confirmation_pending'])
        confirmed_at = self.receipt.call_args.kwargs['paid_at']
        self.repair.assert_called_once_with(job)
        main._repair_jobs.clear()
        self.advance(31)
        self.receipt.return_value = True
        main._repair_payment_tick()
        self.assertFalse(main._repair_job_get(job)['payment_confirmation_pending'])
        self.assertEqual(self.receipt.call_args.kwargs['paid_at'], confirmed_at)
        count = self.receipt.call_count
        main._repair_payment_tick()
        self.assertEqual(self.receipt.call_count, count)
        self.query.assert_not_called()

    def test_existing_receipt_false_result_is_not_retried_as_an_error(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.receipt.return_value = False
        with patch('app.domain.payment_email_repository.PaymentEmailRepository.get', return_value={'status': 'sent'}):
            self.callback(job)
        self.assertFalse(main._repair_job_get(job)['payment_confirmation_pending'])
        self.advance()
        main._repair_payment_tick()
        self.receipt.assert_called_once()

    def test_disabled_or_test_receipts_do_not_create_retry_loops(self):
        for test in (False, True):
            job = self.upload()['job_id']
            self.pay(job, data={'admin_key': 'offline-admin'} if test else {})
            with patch.dict(os.environ, {'OWNER_PAYMENT_EMAIL_ENABLED': '1' if test else '0'}):
                self.callback(job, amount='0.01' if test else '2.99')
            self.assertFalse(main._repair_job_get(job)['payment_confirmation_pending'])
            self.assertEqual(main._repair_job_get(job)['confirmation_delivery'], 'not_required')
        self.receipt.assert_not_called()

    def test_historical_confirmed_order_does_not_gain_a_receipt_retry_marker(self):
        job = self.upload()['job_id']
        main._repair_job_set(job, status='paid', expected_amount='2.99')
        self.callback(job)
        main._repair_payment_tick()
        self.assertFalse(main._repair_job_get(job).get('payment_confirmation_pending'))
        self.receipt.assert_not_called()

    def test_persistent_receipt_failure_stops_after_bounded_retries(self):
        job = self.upload()['job_id']
        self.pay(job)
        self.receipt.return_value = False
        with patch('app.domain.payment_email_repository.PaymentEmailRepository.get', return_value=None):
            self.callback(job)
            for _ in range(10):
                self.advance(301)
                main._repair_payment_tick()
        self.assertEqual(self.receipt.call_count, 8)
        self.assertEqual(main._repair_job_get(job)['status'], 'paid')
        self.assertFalse(main._repair_job_get(job)['payment_confirmation_pending'])
        self.assertEqual(main._repair_job_get(job)['confirmation_delivery'], 'attention_required')

    def test_notification_exception_never_stops_confirmed_repair(self):
        job = self.upload()['job_id']
        self.pay(job)
        with patch.object(main, '_repair_publish_confirmation', side_effect=RuntimeError('storage unavailable')):
            self.assertEqual(self.callback(job).text, 'success')
        self.repair.assert_called_once_with(job)
        self.assertEqual(main._repair_job_get(job)['status'], 'paid')
        self.assertTrue(main._repair_job_get(job)['payment_confirmation_pending'])

    def test_worker_start_is_idempotent_and_shutdown_stops_it(self):
        tick = Mock()
        worker = RepairPaymentWorker(tick)
        fake = Mock()
        fake.is_alive.return_value = True
        with patch('app.domain.repair_payment_worker.threading.Thread', return_value=fake) as thread:
            worker.start()
            worker.start()
            thread.assert_called_once()
            worker.stop()
            self.assertTrue(worker._stop.is_set())
            fake.join.assert_called_once()


if __name__ == '__main__':
    unittest.main()
