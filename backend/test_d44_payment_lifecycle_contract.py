"""R6 late-payment HTTP contract; real local state, fake gateway/broker only."""
import copy
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_d43_dispatch_contract as fixtures


class PaymentLifecycleContractTests(unittest.TestCase):
    # Reuse isolation/helpers, not inheritance: R5's old cancelled-order
    # expectations are intentionally not inherited into the new R6 policy.
    _patch = fixtures.DispatchContractTests._patch
    tearDown = fixtures.DispatchContractTests.tearDown
    job = fixtures.DispatchContractTests.job
    callback = fixtures.DispatchContractTests.callback
    recover = fixtures.DispatchContractTests.recover
    trade = fixtures.DispatchContractTests.trade

    def setUp(self):
        fixtures.DispatchContractTests.setUp(self)
        self.client.app.add_api_route('/jobs/{job_id}', self.main.get_job_v2, methods=['GET'])
        self.client.app.add_api_route('/batches/{batch_id}', self.main.get_batch_v2, methods=['GET'])
        self.source_bytes = self.source.read_bytes()
        self.addCleanup(lambda: self.assertEqual(self.source.read_bytes(), self.source_bytes))

    def expired(self, key='book', **values):
        self.job(key, **values)
        self.assertTrue(self.store.mark_payment_timeout(key, gateway_confirmed=True))
        current = self.store.get(key)
        self.assertEqual(current.status, self.Status.cancelled)
        self.assertEqual(current.error_code, 'PAYMENT_EXPIRED')
        self.assertEqual(current.payment_resolution['state'], 'closed')
        return current

    def cancelled(self, key='book', message='用户明确取消，不再继续', **values):
        job = self.job(key, **values)
        self.store.update_status(key, self.Status.cancelled, message, error_code='USER_CANCELLED')
        return self.store.get(job.id)

    def detail(self, key='book', *, batch=False):
        response = self.client.get(('/batches/' if batch else '/jobs/') + key,
                                   headers={'X-Job-Token': 'owner-only'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_review(self, key, original_message, original_error='USER_CANCELLED'):
        current = self.Store(self.engine).get(key)
        self.assertEqual(current.status, self.Status.cancelled)
        self.assertEqual(current.error_code, 'PAYMENT_REVIEW_REQUIRED')
        resolution = current.payment_resolution
        self.assertEqual(resolution['state'], 'paid_review')
        self.assertEqual(resolution['original_cancel_message'], original_message)
        self.assertEqual(resolution['original_error_code'], original_error)
        self.assertIn('尚未退款', current.message)
        public = self.detail(key)
        self.assertEqual(public['payment_resolution'], resolution)
        self.assertEqual(public['error_code'], 'PAYMENT_REVIEW_REQUIRED')
        self.assertIsNone(public['download_url'])
        return current

    def test_expired_single_callback_releases_once_with_same_frozen_quote(self):
        expired = self.expired(enable_translation=True)
        frozen = copy.deepcopy(expired.payment_entitlement)
        for status in ('TRADE_SUCCESS', 'TRADE_FINISHED', 'TRADE_SUCCESS'):
            self.assertEqual(self.callback(trade_status=status).text, 'success')
        paid = self.Store(self.engine).get('book')
        self.assertEqual(paid.status, self.Status.pending)
        self.assertIsNone(paid.error_code)
        self.assertEqual(paid.payment_resolution['state'], 'paid')
        self.assertIn('closed_at', paid.payment_resolution)
        self.assertEqual(paid.payment_entitlement['state'], 'paid')
        self.assertEqual(paid.payment_entitlement['amount'], frozen['amount'])
        self.assertEqual(paid.expected_amount, expired.expected_amount)
        rows = self.store.list_dispatches('book')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'sent')
        self.publish.assert_called_once_with('book', paid.translation_stats['attempt_id'])
        self.query.assert_not_called()

    def test_expired_single_recover_requires_fresh_matching_gateway_evidence(self):
        self.expired()
        for trade in (None, self.trade('wrong'), self.trade(amount='0.01'),
                      self.trade(trade_status='WAIT_BUYER_PAY'), self.trade(trade_status='TRADE_CLOSED')):
            self.query.return_value = trade
            response = self.recover()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['recovered'])
            self.assertEqual(self.store.get('book').payment_resolution['state'], 'closed')
            self.assertEqual(self.store.list_dispatches('book'), [])
        self.query.return_value = self.trade()
        response = self.recover()
        self.assertTrue(response.json()['recovered'], response.text)
        self.assertEqual(self.store.get('book').status, self.Status.pending)
        self.publish.assert_called_once_with('book', '')

    def test_unknown_cancelled_order_goes_to_persistent_visible_review(self):
        cancelled = self.cancelled(enable_translation=True)
        self.assertEqual(self.callback().text, 'success')
        current = self.assert_review('book', cancelled.message)
        self.assertEqual(current.payment_entitlement['state'], 'paid')
        self.assertEqual(self.store.list_dispatches('book'), [])
        self.publish.assert_not_called()

    def test_actual_owner_cancel_is_not_restarted_by_late_callback(self):
        job = self.job(status=self.Status.pending, enable_translation=True)
        self.grant(self.store, job, '1.99', 'verified_query')
        response = self.client.post('/jobs/book/cancel', headers={'X-Job-Token': 'owner-only'})
        self.assertEqual(response.status_code, 200, response.text)
        original = self.store.get('book')
        self.assertEqual(original.status, self.Status.cancelled)
        self.assertEqual(self.callback().text, 'success')
        self.assert_review('book', original.message, original.error_code)
        self.assertTrue(all(row['status'] == 'obsolete' for row in self.store.list_dispatches('book')))
        self.publish.assert_not_called()

    def test_review_duplicate_callback_and_recovery_preserve_original_cancellation(self):
        cancelled = self.cancelled()
        self.query.return_value = self.trade()
        first = self.recover()
        self.assertTrue(first.json()['recovered'], first.text)
        self.assertEqual(first.json()['payment_resolution']['state'], 'paid_review')
        self.query.assert_called_once_with('book')
        snapshot = copy.deepcopy(self.assert_review('book', cancelled.message).payment_resolution)
        self.assertEqual(self.callback().text, 'success')
        repeat = self.recover()
        self.assertFalse(repeat.json()['recovered'])
        self.assertEqual(repeat.json()['payment_resolution'], snapshot)
        self.query.assert_called_once_with('book')
        self.publish.assert_not_called()

    def test_non_timeout_message_containing_timeout_words_is_not_auto_released(self):
        cancelled = self.cancelled(message='用户取消：此前提示支付超时，订单已关闭，但我不再继续')
        self.callback()
        self.assert_review('book', cancelled.message)
        self.publish.assert_not_called()

    def test_exact_legacy_timeout_messages_can_be_recovered_without_broad_matching(self):
        for index, message in enumerate(('支付超时，订单已关闭', '支付超时，批次订单已关闭')):
            key = f'legacy-{index}'
            self.job(key, status=self.Status.cancelled, message=message)
            self.assertEqual(self.callback(key).text, 'success')
            self.assertEqual(self.store.get(key).status, self.Status.pending)
        self.assertEqual(self.publish.call_count, 2)

    def test_invalid_webhooks_cannot_release_or_mark_review(self):
        self.expired('expired')
        self.cancelled('user-cancel')
        for key in ('expired', 'user-cancel'):
            before = self.store.get(key)
            snapshot = (before.status, before.error_code, before.message, copy.deepcopy(before.payment_resolution),
                        copy.deepcopy(before.payment_entitlement))
            self.verify.return_value = False
            self.assertEqual(self.callback(key).text, 'fail')
            self.verify.return_value = True
            for params in ({'app_id': 'wrong'}, {'seller_id': 'wrong'}, {'total_amount': '0.01'},
                           {'total_amount': 'nan'}, {'trade_status': 'WAIT_BUYER_PAY'}, {'trade_status': 'TRADE_CLOSED'}):
                self.callback(key, **params)
            self.callback('unknown-order')
            after = self.store.get(key)
            self.assertEqual((after.status, after.error_code, after.message, after.payment_resolution,
                              after.payment_entitlement), snapshot)
        self.publish.assert_not_called()
        self.assertEqual(self.store.list_dispatches(), [])

    def test_recover_unauthorized_cancelled_owner_cannot_query_or_change_state(self):
        self.expired()
        self.query.return_value = self.trade()
        self.assertEqual(self.recover(token='wrong').status_code, 403)
        self.query.assert_not_called()
        self.publish.assert_not_called()
        self.assertEqual(self.store.get('book').payment_resolution['state'], 'closed')

    def test_success_failed_running_jobs_are_not_restarted_or_reclassified(self):
        for status in (self.Status.success, self.Status.failed, self.Status.running):
            key = status.value
            self.job(key, status=status, message='Original execution state')
            self.assertEqual(self.callback(key).text, 'success')
            self.assertFalse(self.recover(key).json()['recovered'])
            current = self.store.get(key)
            self.assertEqual((current.status, current.message), (status, 'Original execution state'))
            self.assertNotEqual(current.payment_resolution.get('state'), 'paid_review')
        self.publish.assert_not_called()
        self.query.assert_not_called()

    def batch(self, name='mixed'):
        for index in range(3):
            self.job(f'{name}-{index}', batch_id=name, batch_index=index, batch_size=3,
                     expected_amount='5.97' if index == 0 else '')
        self.assertTrue(self.store.mark_payment_timeout(f'{name}-0', gateway_confirmed=True))
        self.store.update_status(f'{name}-1', self.Status.cancelled, '用户取消第二本', error_code='USER_CANCELLED')

    def assert_mixed_batch(self, name='mixed'):
        self.assertEqual(self.store.get(f'{name}-0').status, self.Status.pending)
        self.assertEqual(self.store.get(f'{name}-2').status, self.Status.pending)
        reviewed = self.assert_review(f'{name}-1', '用户取消第二本')
        rows = {row['job_id']: row for row in self.store.list_dispatches()}
        self.assertEqual(set(rows), {f'{name}-0', f'{name}-2'})
        self.assertTrue(all(row['status'] == 'sent' for row in rows.values()))
        children = {child['job_id']: child for child in self.detail(name, batch=True)['jobs']}
        self.assertEqual(children[reviewed.id]['payment_resolution'], reviewed.payment_resolution)
        self.assertEqual(children[reviewed.id]['error_code'], 'PAYMENT_REVIEW_REQUIRED')
        self.assertIsNone(children[reviewed.id]['download_url'])

    def test_batch_callback_preserves_per_child_expiry_and_user_cancellation(self):
        self.batch()
        for _ in range(2):
            self.assertEqual(self.callback('batch_mixed', total_amount='5.97').text, 'success')
        self.assert_mixed_batch()
        self.assertEqual(self.publish.call_count, 2)
        self.query.assert_not_called()

    def test_batch_recovery_uses_matching_receipt_and_preserves_child_review(self):
        self.batch()
        self.query.return_value = self.trade('batch_mixed', '5.97')
        response = self.recover('mixed', batch=True)
        self.assertTrue(response.json()['recovered'], response.text)
        self.assert_mixed_batch()
        self.query.assert_called_once_with('batch_mixed')
        self.assertEqual(self.publish.call_count, 2)

    def test_batch_pending_outbox_does_not_skip_cancelled_child_reconciliation(self):
        self.job('mixed-0', status=self.Status.pending, batch_id='mixed', batch_index=0, batch_size=2,
                 expected_amount='3.98')
        self.cancelled('mixed-1', batch_id='mixed', batch_index=1, batch_size=2, expected_amount='')
        self.query.return_value = self.trade('batch_mixed', '3.98')
        response = self.recover('mixed', batch=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.query.assert_called_once_with('batch_mixed')
        self.assert_review('mixed-1', '用户明确取消，不再继续')
        self.publish.assert_called_once_with('mixed-0', '')

    def test_all_reviewed_batch_recover_does_not_query_gateway_again(self):
        for index in range(2):
            self.cancelled(f'all-{index}', batch_id='all', batch_index=index, batch_size=2,
                           expected_amount='3.98' if index == 0 else '')
        self.callback('batch_all', total_amount='3.98')
        response = self.recover('all', batch=True)
        self.assertFalse(response.json()['recovered'])
        self.assertTrue(all(child['payment_resolution']['state'] == 'paid_review' for child in response.json()['jobs']))
        self.query.assert_not_called()
        self.publish.assert_not_called()

    def test_invalid_batch_receipts_do_not_release_or_review_any_child(self):
        self.batch()
        before = {job.id: (job.status, job.message, copy.deepcopy(job.payment_resolution)) for job in self.store.list_jobs()}
        for receipt in (None, self.trade('wrong', '5.97'), self.trade('batch_mixed', '0.01'),
                        self.trade('batch_mixed', '5.97', trade_status='TRADE_CLOSED')):
            self.query.return_value = receipt
            self.assertFalse(self.recover('mixed', batch=True).json()['recovered'])
        self.verify.return_value = False
        self.callback('batch_mixed', total_amount='5.97')
        self.verify.return_value = True
        for fields in ({'total_amount': '0.01'}, {'app_id': 'wrong'}, {'seller_id': 'wrong'}):
            self.callback('batch_mixed', **{'total_amount': '5.97', **fields})
        after = {job.id: (job.status, job.message, job.payment_resolution) for job in self.store.list_jobs()}
        self.assertEqual(before, after)
        self.publish.assert_not_called()
        self.assertEqual(self.store.list_dispatches(), [])

    def test_broker_failure_after_late_payment_remains_durable_and_relay_recoverable(self):
        self.expired(enable_translation=True)
        self.publish.side_effect = TimeoutError('broker credentials must stay private')
        self.assertEqual(self.callback().text, 'success')
        current = self.Store(self.engine).get('book')
        self.assertEqual(current.status, self.Status.pending)
        self.assertEqual(current.payment_resolution['state'], 'paid')
        row = self.store.list_dispatches('book')[0]
        self.assertEqual((row['status'], row['attempts']), ('pending', 1))
        self.assertNotIn('credentials', row['last_error'])
        self.publish.side_effect = None
        counts = self.drain(self.Store(self.engine), self.publish, now=row['next_attempt_at'] + 1)
        self.assertEqual(counts['sent'], 1)
        self.assertEqual(self.publish.call_count, 2)
        self.query.assert_not_called()

    def test_late_callback_and_recovery_race_creates_only_one_dispatch(self):
        self.expired(enable_translation=True)
        self.query.return_value = self.trade()
        barrier = threading.Barrier(2)
        settle = self.store.settle_verified_payment
        def simultaneous(*args, **kwargs):
            barrier.wait(timeout=5)
            return settle(*args, **kwargs)
        with patch.object(self.store, 'settle_verified_payment', side_effect=simultaneous), ThreadPoolExecutor(max_workers=2) as executor:
            callback = executor.submit(self.callback)
            recover = executor.submit(self.recover)
            self.assertEqual(callback.result(timeout=8).text, 'success')
            self.assertEqual(recover.result(timeout=8).status_code, 200)
        self.assertEqual(self.store.get('book').status, self.Status.pending)
        self.assertEqual(len(self.store.list_dispatches('book')), 1)
        self.publish.assert_called_once()

    def test_gateway_close_observation_cannot_cancel_already_settled_payment(self):
        self.job()
        self.assertEqual(self.callback().text, 'success')
        self.assertFalse(self.store.mark_payment_timeout('book', gateway_confirmed=True))
        self.assertEqual(self.store.get('book').status, self.Status.pending)
        self.assertEqual(self.store.get('book').payment_resolution['state'], 'paid')
        self.publish.assert_called_once()


if __name__ == '__main__':
    unittest.main()
