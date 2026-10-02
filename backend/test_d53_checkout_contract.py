"""D53 HTTP checkout resumption: real SQLite state, stub gateway/broker boundaries.

No FastAPI startup, live provider, model, worker or customer files are used.
Identity-provider fixtures test owner authorization; task-token validation and
all payment/order transitions run through the real application and store.
"""
from __future__ import annotations

import copy
import os
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_d43_dispatch_contract as fixtures


class CheckoutContractTests(unittest.TestCase):
    _patch = fixtures.DispatchContractTests._patch
    tearDown = fixtures.DispatchContractTests.tearDown

    def setUp(self):
        fixtures.DispatchContractTests.setUp(self)
        self.client.app.add_api_route('/jobs/{job_id}/continue-payment', self.main.continue_job_payment_v2, methods=['POST'])
        self.client.app.add_api_route('/batches/{batch_id}/continue-payment', self.main.continue_batch_payment_v2, methods=['POST'])
        self.client.app.add_api_route('/jobs/{job_id}', self.main.get_job_v2, methods=['GET'])
        self.client.app.add_api_route('/legacy/jobs/{job_id}', self.main.get_job, methods=['GET'])
        self.client.app.add_api_route('/jobs', self.main.create_job_v2, methods=['POST'])
        self.client.app.add_api_route('/batches', self.main.create_batch_v2, methods=['POST'])
        self.client.app.add_api_route('/jobs/{job_id}/confirm-profile', self.main.confirm_translation_profile_v2, methods=['POST'])
        self.checkout_query = self._patch(patch('app.infra.alipay.query_checkout_trade', return_value=self.trade()))
        self.sign = self._patch(patch.object(self.main, 'create_alipay_page_pay', return_value='https://offline.invalid/original-order'))
        self.precreate = self._patch(patch('app.infra.alipay.create_alipay_precreate', side_effect=AssertionError('No precreate during resume')))
        self.owner = self._patch(patch.object(self.main, 'get_current_user_optional', return_value=None))
        self.price = self._patch(patch.object(self.main, '_calc_translation_price', side_effect=AssertionError('Never reprice existing order')))
        self._patch(patch.dict(os.environ, {'RECONCILE_TIMEOUT_HOURS': '2'}))
        self.source_bytes = self.source.read_bytes()
        self.addCleanup(lambda: self.assertEqual(self.source.read_bytes(), self.source_bytes))

    def job(self, key='book', **values):
        values.setdefault('token_expires_at', datetime.now(timezone.utc) + timedelta(hours=4))
        values.setdefault('translation_stats', {'payment_checkout': {
            'schema_version': 1, 'channel': 'page', 'order_no': key,
            'amount': values.get('expected_amount', '1.99'),
        }})
        return fixtures.DispatchContractTests.job(self, key, **values)

    def batch(self, **overrides):
        overrides.setdefault('translation_stats', {'payment_checkout': {
            'schema_version': 1, 'channel': 'page', 'order_no': 'batch_group', 'amount': '5.97',
        }})
        return [self.job(f'child-{index}', batch_id='group', batch_index=index, batch_size=3,
                         expected_amount='5.97' if index == 0 else '', **overrides) for index in range(3)]

    @staticmethod
    def trade(order='book', state='NOT_CREATED', amount='1.99'):
        return {'out_trade_no': order, 'trade_status': state, 'total_amount': amount}

    def resume(self, key='book', *, batch=False, headers=None):
        return self.client.post(('/batches/' if batch else '/jobs/') + key + '/continue-payment',
                                headers={'X-Job-Token': 'owner-only'} if headers is None else headers)

    def assert_no_link(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        self.assertFalse(response.json().get('checkout_available', False))
        self.assertIsNone(response.json().get('pay_url'))

    def test_valid_timed_token_resigns_original_id_and_frozen_amount_without_job_mutation(self):
        before = self.job()
        for state in ('NOT_CREATED', 'WAIT_BUYER_PAY'):
            self.checkout_query.return_value = self.trade(state=state)
            response = self.resume()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['amount'], '1.99')
            self.assertTrue(response.json()['checkout_available'])
            self.assertEqual(response.json()['pay_url'], 'https://offline.invalid/original-order')
            self.assertEqual(self.sign.call_args.kwargs['out_trade_no'], 'book')
            self.assertEqual(self.sign.call_args.kwargs['total_amount'], '1.99')
            after = self.Store(self.engine).get('book')
            self.assertEqual(after, before)
        self.assertEqual(len(self.store.list_jobs()), 1)
        self.assertEqual(self.store.list_dispatches(), [])
        self.precreate.assert_not_called()
        self.price.assert_not_called()

    def test_active_owner_does_not_need_token_but_foreign_or_disabled_owner_cannot_resume(self):
        from app.models import User
        self.job(user_id='owner')
        self.owner.return_value = User(id='owner', is_active=True)
        self.assertEqual(self.resume(headers={}).status_code, 200)
        self.owner.return_value = User(id='foreign', is_active=True)
        self.assertEqual(self.resume(headers={}).status_code, 403)
        self.owner.return_value = User(id='owner', is_active=False)
        self.assertEqual(self.resume().status_code, 403)
        self.checkout_query.assert_called_once_with('book')

    def test_missing_wrong_expired_or_expiryless_token_cannot_use_ip_session_fallback(self):
        for index, options in enumerate(({}, {'access_token': ''}, {'token_expires_at': None},
                                        {'token_expires_at': datetime.now(timezone.utc) - timedelta(seconds=1)})):
            key = str(index)
            self.job(key, creator_ip='testclient', creator_session='same-session', **options)
            for headers in ({}, {'X-Job-Token': 'wrong'}, {'X-Client-Session': 'same-session'}):
                with self.subTest(options=options, headers=headers):
                    self.assertEqual(self.resume(key, headers=headers).status_code, 403)
            if options:
                self.assertEqual(self.resume(key).status_code, 403)
        self.checkout_query.assert_not_called()
        self.sign.assert_not_called()

    def test_query_and_job_cookie_tokens_obey_same_strict_capability(self):
        self.job()
        query = self.client.post('/jobs/book/continue-payment?token=owner-only')
        self.assertEqual(query.status_code, 200, query.text)
        self.client.cookies.set('job_token_book', 'owner-only')
        response = self.client.post('/jobs/book/continue-payment')
        self.assertEqual(response.status_code, 200, response.text)

    def test_queued_running_and_completed_jobs_return_no_link_without_gateway_or_signer(self):
        for status in (self.Status.pending, self.Status.running, self.Status.success):
            job = self.job(status.value, status=status, output_path=str(self.source) if status == self.Status.success else None)
            before = copy.deepcopy(job)
            self.assert_no_link(self.resume(job.id))
            self.assertEqual(self.store.get(job.id), before)
        self.checkout_query.assert_not_called()
        self.sign.assert_not_called()
        self.publish.assert_not_called()

    def test_cancelled_failed_or_unconfirmed_jobs_cannot_generate_link(self):
        for status in (self.Status.cancelled, self.Status.failed, self.Status.awaiting_confirmation, self.Status.confirming):
            self.job(status.value, status=status)
            self.assert_no_link(self.resume(status.value), 409)
        self.checkout_query.assert_not_called()
        self.sign.assert_not_called()

    def test_batch_child_is_never_resigned_as_a_separate_order(self):
        self.batch()
        self.assert_no_link(self.resume('child-0'), 409)
        self.assert_no_link(self.resume('child-2'), 409)
        self.checkout_query.assert_not_called()
        self.sign.assert_not_called()

    def test_unknown_or_wrong_gateway_identity_returns_503_without_mutation(self):
        before = self.job()
        for result in (None, {}, self.trade(order='foreign'), self.trade(state='UNKNOWN')):
            self.checkout_query.return_value = result
            self.assert_no_link(self.resume(), 503)
            self.assertEqual(self.store.get('book'), before)
        self.sign.assert_not_called()
        self.publish.assert_not_called()

    def test_waiting_or_paid_amount_mismatch_never_signs_or_releases(self):
        self.job(enable_translation=True)
        for status in ('WAIT_BUYER_PAY', 'TRADE_SUCCESS', 'TRADE_FINISHED'):
            for amount in ('0.01', '', 'NaN', 'Infinity', '-1', None):
                self.checkout_query.return_value = self.trade(state=status, amount=amount)
                self.assert_no_link(self.resume(), 409)
        self.assertEqual(self.store.get('book').status, self.Status.pending_payment)
        self.assertEqual(self.store.list_dispatches(), [])
        self.sign.assert_not_called()
        self.publish.assert_not_called()

    def test_paid_receipt_settles_real_store_and_only_one_durable_dispatch_survives_repeats(self):
        self.job(enable_translation=True)
        self.checkout_query.return_value = self.trade(state='TRADE_SUCCESS')
        for _ in range(3):
            self.assert_no_link(self.resume())
        current = self.Store(self.engine).get('book')
        self.assertEqual(current.status, self.Status.pending)
        self.assertEqual(current.expected_amount, '1.99')
        self.assertEqual(current.payment_entitlement['state'], 'paid')
        self.assertEqual(current.payment_resolution['state'], 'paid')
        self.assertEqual(len(self.store.list_dispatches('book')), 1)
        self.assertEqual(self.store.list_dispatches('book')[0]['status'], 'sent')
        self.publish.assert_called_once_with('book', current.translation_stats['attempt_id'])
        self.checkout_query.assert_called_once_with('book')
        self.sign.assert_not_called()

    def test_closed_proof_marks_expired_but_does_not_sign_or_claim_refund(self):
        self.job()
        self.checkout_query.return_value = self.trade(state='TRADE_CLOSED')
        self.assert_no_link(self.resume(), 409)
        current = self.store.get('book')
        self.assertEqual(current.status, self.Status.cancelled)
        self.assertEqual(current.error_code, 'PAYMENT_EXPIRED')
        self.assertEqual(current.payment_resolution['state'], 'closed')
        self.assertNotIn('refunded', current.payment_resolution)
        self.sign.assert_not_called()

    def test_local_expiry_does_not_sign_or_fabricate_gateway_closure(self):
        before = self.job(created_at=datetime.now(timezone.utc) - timedelta(hours=2, seconds=1))
        self.assert_no_link(self.resume(), 409)
        after = self.store.get('book')
        self.assertEqual(after, before)
        self.assertEqual(after.status, self.Status.pending_payment)
        self.assertFalse(after.payment_resolution)
        self.sign.assert_not_called()

    def test_invalid_frozen_amount_never_queries_gateway_or_reprices(self):
        for index, amount in enumerate(('', 'NaN', 'Infinity', '0', '-1', '1.234', 'nonsense', '1.99e0', '+1.99')):
            self.job(str(index), expected_amount=amount)
            self.assert_no_link(self.resume(str(index)), 409)
        self.checkout_query.assert_not_called()
        self.sign.assert_not_called()
        self.price.assert_not_called()

    def test_missing_or_empty_source_refuses_payment_without_changing_order(self):
        empty = self.root / 'empty.epub'; empty.write_bytes(b'')
        for index, path in enumerate((self.root / 'missing.epub', empty)):
            before = self.job(str(index), input_path=str(path))
            self.checkout_query.return_value = self.trade(order=str(index))
            self.assert_no_link(self.resume(str(index)), 409)
            self.assertEqual(self.store.get(str(index)), before)
        self.sign.assert_not_called()

    def test_signing_failure_or_empty_url_preserves_original_pending_order(self):
        before = self.job()
        for error in (TimeoutError('private secret'), None):
            self.sign.side_effect = error
            self.sign.return_value = ''
            self.assert_no_link(self.resume(), 503)
            self.assertEqual(self.store.get('book'), before)
        self.publish.assert_not_called()

    def test_invalid_or_credential_bearing_payment_url_is_never_returned(self):
        before = self.job()
        for url in ('javascript:alert(1)', '//offline.invalid/pay', 'http://',
                    'https://user:secret@offline.invalid/pay', 'https://user@offline.invalid/pay', None, {}):
            with self.subTest(url=url):
                self.sign.return_value = url
                self.assert_no_link(self.resume(), 503)
                self.assertEqual(self.store.get('book'), before)

    def test_payment_while_broker_unavailable_still_has_one_durable_intent(self):
        self.job(enable_translation=True)
        self.checkout_query.return_value = self.trade(state='TRADE_SUCCESS')
        self.publish.side_effect = ConnectionError('offline broker unavailable')
        self.assert_no_link(self.resume())
        self.assert_no_link(self.resume())
        rows = self.Store(self.engine).list_dispatches('book')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'pending')
        self.assertEqual(self.store.get('book').payment_resolution['state'], 'paid')
        self.publish.assert_called_once()
        self.checkout_query.assert_called_once()
        self.sign.assert_not_called()

    def test_concurrent_paid_recovery_keeps_one_job_intent_and_transport(self):
        self.job(enable_translation=True)
        barrier = threading.Barrier(2)
        def query(_order):
            barrier.wait(timeout=5)
            return self.trade(state='TRADE_SUCCESS')
        self.checkout_query.side_effect = query
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.resume) for _ in range(2)]
            for future in futures:
                self.assert_no_link(future.result(timeout=10))
        self.assertEqual(len(self.store.list_jobs()), 1)
        self.assertEqual(len(self.store.list_dispatches('book')), 1)
        self.publish.assert_called_once()
        self.sign.assert_not_called()

    def test_frozen_amount_changed_during_query_or_signing_never_returns_link(self):
        from sqlalchemy import update
        from app.storage_db import JobRecord
        for phase in ('query', 'sign'):
            self.job(phase)
            def change_price():
                with self.store._Session() as session:
                    session.execute(update(JobRecord).where(JobRecord.id == phase).values(expected_amount='9.99'))
                    session.commit()
            def query(_order):
                if phase == 'query':
                    change_price()
                return self.trade(order=phase)
            def sign(**_kwargs):
                change_price()
                return 'https://offline.invalid/stale-price'
            self.checkout_query.side_effect = query
            self.sign.side_effect = sign
            self.assert_no_link(self.resume(phase), 409)
        self.assertEqual(self.sign.call_count, 1)
        self.price.assert_not_called()

    def test_paid_or_cancelled_during_query_never_reaches_signer(self):
        for status in (self.Status.pending, self.Status.cancelled):
            key = status.value
            self.job(key)
            def query(_order):
                self.store.update_status(key, status, 'concurrent callback or cancellation')
                return self.trade(order=key)
            self.checkout_query.side_effect = query
            self.assert_no_link(self.resume(key), 200 if status == self.Status.pending else 409)
            self.assertEqual(self.store.get(key).status, status)
        self.sign.assert_not_called()

    def test_closed_response_cannot_overwrite_simultaneous_paid_callback(self):
        self.job()
        def query(_order):
            self.store.settle_verified_payment('book', amount='1.99', source='verified_webhook')
            return self.trade(state='TRADE_CLOSED')
        self.checkout_query.side_effect = query
        self.assert_no_link(self.resume())
        current = self.store.get('book')
        self.assertEqual(current.status, self.Status.pending)
        self.assertEqual(current.payment_resolution['state'], 'paid')
        self.assertNotEqual(current.error_code, 'PAYMENT_EXPIRED')
        self.sign.assert_not_called()

    def test_paid_cancelled_or_completed_during_signing_suppresses_stale_link(self):
        for status in (self.Status.pending, self.Status.cancelled, self.Status.success):
            key = status.value
            self.job(key)
            self.checkout_query.return_value = self.trade(order=key)
            def sign(**_kwargs):
                self.store.update_status(key, status, 'concurrent change',
                                         output_path=str(self.source) if status == self.Status.success else None)
                return 'https://offline.invalid/stale-payment-link'
            self.sign.side_effect = sign
            self.assert_no_link(self.resume(key), 409 if status == self.Status.cancelled else 200)
            current = self.store.get(key)
            self.assertEqual(current.status, status)
            if status == self.Status.success:
                self.assertEqual(current.output_path, str(self.source))
        self.publish.assert_not_called()

    def test_batch_uses_only_leader_total_and_original_merchant_order(self):
        before = self.batch()
        self.checkout_query.return_value = self.trade(order='batch_group', state='WAIT_BUYER_PAY', amount='5.97')
        response = self.resume('group', batch=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['amount'], '5.97')
        self.assertTrue(response.json()['checkout_available'])
        self.checkout_query.assert_called_once_with('batch_group')
        self.assertEqual(self.sign.call_args.kwargs['out_trade_no'], 'batch_group')
        self.assertEqual(self.sign.call_args.kwargs['total_amount'], '5.97')
        self.assertEqual(self.store.list_jobs_by_batch_id('group'), before)
        self.assertEqual(len(self.store.list_jobs()), 3)
        self.precreate.assert_not_called()

    def test_known_qr_checkout_returns_only_saved_code_without_crossing_payment_products(self):
        before = self.job(translation_stats={'payment_checkout': {
            'schema_version': 1, 'channel': 'qr', 'order_no': 'book', 'amount': '1.99',
            'qr_code': 'https://offline.invalid/original-qr',
        }})
        for state in ('NOT_CREATED', 'WAIT_BUYER_PAY'):
            self.checkout_query.return_value = self.trade(state=state)
            response = self.resume()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['checkout_available'])
            self.assertIsNone(response.json()['pay_url'])
            self.assertEqual(response.json()['qr_code'], 'https://offline.invalid/original-qr')
            self.assertEqual(self.store.get('book'), before)
        self.precreate.assert_not_called()
        self.sign.assert_not_called()

    def test_known_batch_qr_keeps_original_total_order_and_saved_code(self):
        self.batch(translation_stats={'payment_checkout': {
            'schema_version': 1, 'channel': 'qr', 'order_no': 'batch_group', 'amount': '5.97',
            'qr_code': 'https://offline.invalid/batch-original-qr',
        }})
        self.checkout_query.return_value = self.trade(order='batch_group', state='WAIT_BUYER_PAY', amount='5.97')
        response = self.resume('group', batch=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['amount'], '5.97')
        self.assertEqual(response.json()['qr_code'], 'https://offline.invalid/batch-original-qr')
        self.assertTrue(response.json()['checkout_available'])
        self.assertIsNone(response.json()['pay_url'])
        self.sign.assert_not_called()
        self.precreate.assert_not_called()

    def test_qr_window_never_exceeds_two_hours_when_reconciliation_window_is_longer(self):
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        qr = self.job('qr', created_at=old, translation_stats={'payment_checkout': {
            'schema_version': 1, 'channel': 'qr', 'order_no': 'qr', 'amount': '1.99',
            'qr_code': 'https://offline.invalid/original-qr',
        }})
        self.job('page', created_at=old)
        with patch.dict(os.environ, {'RECONCILE_TIMEOUT_HOURS': '4'}):
            self.checkout_query.return_value = self.trade(order='qr')
            self.assert_no_link(self.resume('qr'), 409)
            self.assertEqual(self.store.get('qr'), qr)
            self.checkout_query.return_value = self.trade(order='page')
            self.assertEqual(self.resume('page').status_code, 200)
        self.sign.assert_called_once()
        self.precreate.assert_not_called()

    def test_qr_window_honors_a_shorter_reconciliation_limit(self):
        before = self.job(created_at=datetime.now(timezone.utc) - timedelta(minutes=90),
                          translation_stats={'payment_checkout': {
                              'schema_version': 1, 'channel': 'qr', 'order_no': 'book', 'amount': '1.99',
                              'qr_code': 'https://offline.invalid/original-qr',
                          }})
        with patch.dict(os.environ, {'RECONCILE_TIMEOUT_HOURS': '1'}):
            self.assert_no_link(self.resume(), 409)
        self.assertEqual(self.store.get('book'), before)
        self.sign.assert_not_called()
        self.precreate.assert_not_called()

    def test_regular_detail_does_not_leak_saved_or_stale_qr_snapshot(self):
        self.job(translation_stats={'payment_checkout': {
            'schema_version': 1, 'channel': 'qr', 'order_no': 'book', 'amount': '1.99',
            'qr_code': 'https://offline.invalid/private-old-qr',
        }})
        for url in ('/jobs/book', '/legacy/jobs/book'):
            response = self.client.get(url, headers={'X-Job-Token': 'owner-only'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn('payment_checkout', response.json().get('translation_stats') or {})
            self.assertNotIn('private-old-qr', response.text)
        self.assertIn('payment_checkout', self.store.get('book').translation_stats)
        self.checkout_query.assert_not_called()

    def test_channel_change_during_page_signing_suppresses_old_product_link(self):
        self.job()
        def sign(**_kwargs):
            self.store.update_status('book', self.Status.pending_payment, 'concurrent channel correction',
                                     translation_stats={'payment_checkout': {
                                         'schema_version': 1, 'channel': 'qr', 'order_no': 'book', 'amount': '1.99',
                                         'qr_code': 'https://offline.invalid/original-qr',
                                     }})
            return 'https://offline.invalid/stale-page-link'
        self.sign.side_effect = sign
        self.assert_no_link(self.resume(), 409)
        self.assertEqual(self.store.get('book').translation_stats['payment_checkout']['channel'], 'qr')

    def test_unknown_legacy_conversion_or_batch_channel_never_switches_to_page(self):
        self.job(translation_stats={})
        self.batch(translation_stats={})
        for state in ('NOT_CREATED', 'WAIT_BUYER_PAY'):
            self.checkout_query.return_value = self.trade(state=state)
            self.assert_no_link(self.resume(), 409)
            self.checkout_query.return_value = self.trade(order='batch_group', state=state, amount='5.97')
            self.assert_no_link(self.resume('group', batch=True), 409)
        self.sign.assert_not_called()
        self.precreate.assert_not_called()

    def test_known_legacy_translation_page_channel_remains_usable(self):
        self.job(enable_translation=True, translation_stats={})
        response = self.resume()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['checkout_available'])
        self.assertEqual(response.json()['pay_url'], 'https://offline.invalid/original-order')
        self.assertEqual(self.sign.call_args.kwargs['out_trade_no'], 'book')
        self.precreate.assert_not_called()

    def test_malformed_or_mismatched_channel_snapshot_is_not_payment_permission(self):
        base = {'schema_version': 1, 'channel': 'page', 'order_no': 'book', 'amount': '1.99'}
        snapshots = (None, [], 'page', {}, {**base, 'schema_version': 2},
                     {**base, 'channel': 'unknown'}, {**base, 'order_no': 'foreign'},
                     {**base, 'amount': '0.01'}, {**base, 'channel': 'qr'},
                     {**base, 'channel': 'qr', 'qr_code': ''},
                     {**base, 'channel': 'qr', 'qr_code': {'unsafe': 'not a code'}})
        for index, snapshot in enumerate(snapshots):
            key = 'bad-' + str(index)
            if isinstance(snapshot, dict) and snapshot.get('order_no') == 'book':
                snapshot = {**snapshot, 'order_no': key}
            self.job(key, enable_translation=True, translation_stats={'payment_checkout': snapshot})
            self.checkout_query.return_value = self.trade(order=key)
            with self.subTest(snapshot=snapshot):
                self.assert_no_link(self.resume(key), 409)
        self.sign.assert_not_called()
        self.precreate.assert_not_called()

    def test_real_single_conversion_creation_persists_its_successful_qr_channel(self):
        self.precreate.side_effect = None
        self.precreate.return_value = 'https://offline.invalid/created-qr'
        response = self.client.post('/jobs', files={'file': ('fixture.epub', self.source_bytes, 'application/epub+zip')},
                                    data={'output_mode': 'simplified'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        job = self.Store(self.engine).get(body['job_id'])
        self.assertEqual(job.translation_stats['payment_checkout'], {
            'schema_version': 1, 'channel': 'qr', 'order_no': job.id,
            'amount': job.expected_amount, 'qr_code': body['qr_code'],
        })
        self.assertEqual(body['qr_code'], 'https://offline.invalid/created-qr')
        self.assertIsNone(body['pay_url'])
        self.sign.assert_not_called()

    def test_real_single_conversion_fallback_persists_page_not_qr_channel(self):
        self.precreate.side_effect = RuntimeError('offline precreate unavailable')
        response = self.client.post('/jobs', files={'file': ('fixture.epub', self.source_bytes, 'application/epub+zip')},
                                    data={'output_mode': 'simplified'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        job = self.Store(self.engine).get(body['job_id'])
        self.assertEqual(job.translation_stats['payment_checkout'], {
            'schema_version': 1, 'channel': 'page', 'order_no': job.id, 'amount': job.expected_amount,
        })
        self.assertEqual(body['pay_url'], 'https://offline.invalid/original-order')

    def test_real_translation_direct_checkout_persists_page_channel(self):
        self.price.side_effect = None
        self.price.return_value = '7.77'
        response = self.client.post('/jobs', files={'file': ('fixture.epub', self.source_bytes, 'application/epub+zip')},
                                    data={'enable_translation': 'true', 'output_mode': 'simplified'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        job = self.Store(self.engine).get(body['job_id'])
        self.assertEqual(job.translation_stats['payment_checkout'], {
            'schema_version': 1, 'channel': 'page', 'order_no': job.id, 'amount': '7.77',
        })
        self.precreate.assert_not_called()

    def test_real_translation_confirmation_persists_page_only_after_explicit_confirm(self):
        self.price.side_effect = None
        self.price.return_value = '7.77'
        with patch.object(self.main, 'build_translation_preflight', return_value={
                'version': 1, 'resolved_strategy': 'neutral_faithful', 'profile': {},
                'characters': [], 'glossary': {}, 'chapters': [], 'confirmed': False}):
            response = self.client.post('/jobs', files={'file': ('fixture.epub', self.source_bytes, 'application/epub+zip')},
                                        data={'enable_translation': 'true', 'profile_confirmation': 'true'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn('payment_checkout', self.store.get(body['job_id']).translation_stats)
        self.sign.assert_not_called()
        confirmed = self.client.post('/jobs/' + body['job_id'] + '/confirm-profile',
                                     headers={'X-Job-Token': body['access_token']}, json={})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        job = self.Store(self.engine).get(body['job_id'])
        self.assertEqual(job.translation_stats['payment_checkout'], {
            'schema_version': 1, 'channel': 'page', 'order_no': job.id, 'amount': '7.77',
        })
        self.sign.assert_called_once()
        self.precreate.assert_not_called()

    def test_real_batch_creation_persists_original_batch_qr_channel(self):
        self.precreate.side_effect = None
        self.precreate.return_value = 'https://offline.invalid/created-batch-qr'
        response = self.client.post('/batches', files=[
            ('files', ('one.epub', self.source_bytes, 'application/epub+zip')),
            ('files', ('two.epub', self.source_bytes, 'application/epub+zip')),
        ], data={'output_mode': 'simplified'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        rows = self.Store(self.engine).list_jobs_by_batch_id(body['batch_id'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].translation_stats['payment_checkout'], {
            'schema_version': 1, 'channel': 'qr', 'order_no': 'batch_' + body['batch_id'],
            'amount': body['amount'], 'qr_code': 'https://offline.invalid/created-batch-qr',
        })
        self.assertEqual(rows[0].expected_amount, body['amount'])
        self.assertEqual(rows[1].expected_amount, '')
        self.sign.assert_not_called()

    def test_batch_paid_receipt_releases_all_children_once(self):
        self.batch()
        self.checkout_query.return_value = self.trade(order='batch_group', state='TRADE_FINISHED', amount='5.97')
        for _ in range(3):
            self.assert_no_link(self.resume('group', batch=True))
        rows = self.Store(self.engine).list_jobs_by_batch_id('group')
        self.assertTrue(all(row.status == self.Status.pending for row in rows))
        self.assertEqual([row.expected_amount for row in rows], ['5.97', '', ''])
        self.assertEqual(len(self.store.list_dispatches()), 3)
        self.assertTrue(all(row['status'] == 'sent' for row in self.store.list_dispatches()))
        self.assertEqual(self.publish.call_count, 3)
        self.checkout_query.assert_called_once_with('batch_group')
        self.sign.assert_not_called()

    def test_batch_mixed_unpaid_and_running_or_cancelled_states_never_sign(self):
        self.batch()
        self.store.update_status('child-1', self.Status.pending, 'concurrent release')
        self.assert_no_link(self.resume('group', batch=True), 409)
        self.store.update_status('child-1', self.Status.cancelled, 'user cancellation')
        self.assert_no_link(self.resume('group', batch=True), 409)
        self.checkout_query.assert_not_called()
        self.sign.assert_not_called()

    def test_batch_authorization_applies_to_every_child(self):
        from sqlalchemy import update
        from app.storage_db import JobRecord
        self.batch()
        with self.store._Session() as session:
            session.execute(update(JobRecord).where(JobRecord.id == 'child-2').values(access_token='foreign-token'))
            session.commit()
        self.assert_no_link(self.resume('group', batch=True), 403)
        self.checkout_query.assert_not_called()
        self.sign.assert_not_called()

    def test_incomplete_batch_or_missing_order_is_rejected_before_gateway(self):
        self.job('only-child', batch_id='group', batch_index=0, batch_size=3)
        self.assert_no_link(self.resume('group', batch=True), 409)
        self.assert_no_link(self.resume('missing'), 404)
        self.assert_no_link(self.resume('missing', batch=True), 404)
        self.checkout_query.assert_not_called()
        self.sign.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
