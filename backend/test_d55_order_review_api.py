"""Private manual-resolution API boundary; no real payment/refund/model calls."""
import json
import os
import unittest
from dataclasses import asdict
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.admin.router import make_router
from app.models import JobStatus
from app.storage_db import JobRecord, OrderReviewEventRecord
import test_d19_order_admin as admin_tests


class ReviewApiTests(unittest.TestCase):
    setUpClass = classmethod(admin_tests.AdminTests.setUpClass.__func__)
    tearDown = admin_tests.AdminTests.tearDown
    job = admin_tests.AdminTests.job
    login = admin_tests.AdminTests.login

    def setUp(self):
        admin_tests.AdminTests.setUp(self)
        self.addCleanup(patch.stopall)
        for target in ("socket.socket.connect", "socket.create_connection"):
            patch(target, side_effect=AssertionError("external networking forbidden")).start()

    def review_job(self, job_id="a", **kwargs):
        values = dict(enable_translation=False, status=JobStatus.cancelled,
                      error_code="PAYMENT_REVIEW_REQUIRED",
                      payment_resolution={"state": "paid_review", "source": "verified_query",
                                          "amount": "5.99", "verified_at": "2026-10-02T01:00:00+00:00"})
        values.update(kwargs)
        return self.job(job_id, **values)

    def payload(self, action="note", job_id="a", **kwargs):
        response = self.client.get(f"/api/admin/orders/{job_id}")
        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()["review"]
        body = dict(action=action, request_id=str(uuid4()),
                    expected_revision=review["revision"], expected_context=review["context"],
                    note="管理员独立核查记录", evidence="私有核查证据")
        body.update(kwargs)
        return body

    def post(self, body, job_id="a", client=None):
        return (client or self.client).post(f"/api/admin/orders/{job_id}/review", json=body)

    def test_auth_csrf_origin_actor_and_input_validation(self):
        self.review_job()
        self.assertEqual(self.client.get('/api/admin/orders/a/review-history').status_code, 401)
        self.login()
        body = self.payload()
        with TestClient(self.app, base_url='https://testserver') as anonymous:
            self.assertEqual(self.post(body, client=anonymous).status_code, 401)
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.post(body).status_code, 403)
        self.login()
        self.client.headers['Origin'] = 'https://untrusted.invalid'
        self.assertEqual(self.post(body).status_code, 403)
        self.client.headers.pop('Origin')
        for changes in ({'actor': 'forged-admin'}, {'request_id': 'bad'},
                        {'expected_revision': -1}, {'expected_context': 'bad'},
                        {'note': 'x' * 4001}, {'action': 'refund'}):
            response = self.post({**body, **changes})
            self.assertEqual(response.status_code, 422, response.text)
        self.trade.assert_not_called()
        with patch.dict(os.environ, {'ADMIN_PASSWORD_HASH': ''}):
            self.assertIn(self.post(body).status_code, (401, 503))

    def test_private_append_only_audit_no_payment_mutation_or_gateway(self):
        original = self.review_job()
        self.login()
        body = self.payload()
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        history = self.client.get('/api/admin/orders/a/review-history')
        self.assertEqual(history.headers['cache-control'], 'no-store')
        event = history.json()['items'][0]
        self.assertEqual(event['actor'], 'tristan')
        self.assertEqual(event['note'], body['note'])
        self.assertEqual(event['evidence'], body['evidence'])
        current = self.store.get('a')
        self.assertEqual(current.status, original.status)
        self.assertEqual(current.expected_amount, original.expected_amount)
        self.assertEqual(current.payment_resolution, original.payment_resolution)
        self.assertNotIn(body['evidence'], json.dumps(asdict(current), default=str, ensure_ascii=False))
        self.trade.assert_not_called()
        self.assertFalse(self.queued)
        for path in ('/orders', '/orders/a'):
            self.assertEqual(self.client.get('/api/admin' + path).headers['cache-control'], 'no-store')

    def test_idempotency_stale_revision_and_changed_payload(self):
        self.review_job(); self.login()
        first = self.payload()
        stale = {**first, 'request_id': str(uuid4())}
        self.assertEqual(self.post(first).status_code, 200)
        duplicate = self.post(first)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(duplicate.json()['review_action']['duplicate'])
        self.assertEqual(self.post({**first, 'note': 'changed'}).status_code, 409)
        self.assertEqual(self.post(stale).status_code, 409)
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(OrderReviewEventRecord)).all()), 1)

    def test_fulfill_requires_fresh_matching_proof(self):
        self.review_job(); self.login()
        body = self.payload('fulfill', acknowledge_cost=True)
        cases = [None,
                 {'out_trade_no': 'other', 'trade_status': 'TRADE_SUCCESS', 'total_amount': '5.99', 'trade_no': 't'},
                 {'out_trade_no': 'a', 'trade_status': 'TRADE_SUCCESS', 'total_amount': '0.01', 'trade_no': 't'},
                 {'out_trade_no': 'a', 'trade_status': 'TRADE_CLOSED', 'total_amount': '5.99', 'trade_no': 't'},
                 {'out_trade_no': 'a', 'trade_status': 'TRADE_SUCCESS', 'total_amount': '5.99'}]
        for trade in cases:
            self.trade.return_value = trade
            response = self.post(body)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.store.get('a').status, JobStatus.cancelled)
        self.trade.side_effect = RuntimeError('private provider message')
        response = self.post(body)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn('private provider', response.text)
        self.assertFalse(self.queued)

    def test_fulfill_once_and_queue_failure_preserves_durable_intent(self):
        self.review_job(); self.login()
        body = self.payload('fulfill', acknowledge_cost=True)
        app = FastAPI()
        def unavailable(job, background):
            raise RuntimeError('broker unavailable')
        app.include_router(make_router(self.store, self.uploads, self.outputs, unavailable))
        with TestClient(app, base_url='https://testserver') as client:
            self.login(client)
            response = self.post(body, client=client)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['review_action']['dispatch_pending'])
        current = self.store.get('a')
        self.assertEqual(current.status, JobStatus.pending)
        self.assertEqual(current.expected_amount, '5.99')
        self.assertEqual(len(self.store.list_dispatches('a')), 1)
        self.trade.reset_mock()
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['review_action']['duplicate'])
        self.trade.assert_not_called()
        self.assertFalse(self.queued)
        self.assertEqual(len(self.store.list_dispatches('a')), 1)

    def test_source_change_requires_new_confirmation_and_paid_review_cannot_close(self):
        job = self.review_job(); self.login()
        body = self.payload('fulfill', acknowledge_cost=True)
        self.assertEqual(self.post({**body, 'action': 'close_review'}).status_code, 409)
        from pathlib import Path
        Path(job.input_path).write_bytes(b'a changed uploaded fixture')
        response = self.post(body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.get('a').status, JobStatus.cancelled)
        self.assertFalse(self.queued)

    def test_malformed_historical_batch_remains_visible_but_cannot_be_processed(self):
        self.review_job('a', batch_id='legacy', batch_index=0, batch_size=3)
        self.review_job('b', batch_id='legacy', batch_index=1, batch_size=3, expected_amount='')
        self.login()
        response = self.client.get('/api/admin/orders')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['total'], 2)
        for item in response.json()['items']:
            self.assertTrue(item['review']['needs_attention'])
            self.assertFalse(item['review']['allowed_actions'])
            self.assertIn('invalid_batch', [r['code'] for r in item['review']['reasons']])
        body = self.payload('record_external_refund', refund_reference='must-not-apply')
        self.assertEqual(self.post(body).status_code, 409)
        self.assertEqual(self.store.get('a').payment_resolution['state'], 'paid_review')

    def test_non_numeric_historical_batch_metadata_has_read_only_diagnostic(self):
        self.login()
        for field in ('batch_size', 'batch_index'):
            with self.subTest(field=field):
                leader, child, batch = field + '-leader', field + '-child', field + '-batch'
                self.review_job(leader, batch_id=batch, batch_index=0, batch_size=2)
                job = self.review_job(child, batch_id=batch, batch_index=1, batch_size=2,
                                      expected_amount='')
                with self.engine.begin() as conn:
                    conn.execute(update(JobRecord).where(JobRecord.id == child).values(
                        **{field: 'private-malformed-canary'}))
                    before = dict(conn.execute(select(JobRecord).where(JobRecord.id == child)).mappings().one())
                listing = self.client.get('/api/admin/orders')
                self.assertEqual(listing.status_code, 200, listing.text)
                listed = next(item for item in listing.json()['items'] if item['id'] == child)
                response = self.client.get('/api/admin/orders/' + child)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                detail = response.json()
                for item in (listed, detail):
                    self.assertEqual(item['id'], child)
                    self.assertEqual(item['filename'], job.source_filename)
                    self.assertEqual(item['status'], 'cancelled')
                    self.assertEqual(item['price_cny'], '5.99')
                    self.assertTrue(item['metadata_invalid'])
                    self.assertEqual(item['review']['scope_count'], 2)
                    self.assertEqual(item['review']['allowed_actions'], [])
                    self.assertEqual(item['review']['reasons'][0]['code'], 'metadata_invalid')
                    self.assertIsNone(item['cost']['estimated_usd'])
                    self.assertIsNone(item['cost']['ledger'])
                    self.assertEqual(item['files'], {'source': False, 'output': False})
                    for private in ('private-malformed-canary', 'must-not-leak', job.input_path, 'ValueError'):
                        self.assertNotIn(private, json.dumps(item, ensure_ascii=False))
                self.assertEqual(detail['stages'], [])
                for path in ('/files/source', '/files/output', '/review-history', '/usage'):
                    rejected = self.client.get('/api/admin/orders/' + child + path)
                    self.assertEqual(rejected.status_code, 409, rejected.text)
                    self.assertEqual(rejected.headers['cache-control'], 'no-store')
                    self.assertNotIn('private-malformed-canary', rejected.text)
                for path, body in (('/payment', {}), ('/retry', {'acknowledge_cost': True})):
                    rejected = self.client.post('/api/admin/orders/' + child + path, json=body)
                    self.assertEqual(rejected.status_code, 409, rejected.text)
                for action in ('note', 'fulfill', 'record_external_refund', 'close_review'):
                    body = dict(action=action, request_id=str(uuid4()), expected_revision=0,
                                expected_context=detail['review']['context'], note='cannot apply',
                                evidence='cannot apply', refund_reference='cannot apply', acknowledge_cost=True)
                    self.assertEqual(self.post(body, child).status_code, 409)
                with self.engine.connect() as conn:
                    after = dict(conn.execute(select(JobRecord).where(JobRecord.id == child)).mappings().one())
                    self.assertEqual(after, before)
                    self.assertEqual(conn.execute(select(OrderReviewEventRecord)).all(), [])
                self.assertEqual(self.store.list_dispatches(child), [])
        self.trade.assert_not_called()
        self.assertFalse(self.queued)

    def test_invalid_standalone_status_preserves_raw_status_and_price(self):
        self.review_job()
        with self.engine.begin() as conn:
            conn.execute(update(JobRecord).where(JobRecord.id == 'a').values(status='historical-status'))
        self.login()
        response = self.client.get('/api/admin/orders/a')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'historical-status')
        self.assertEqual(response.json()['price_cny'], '5.99')
        self.assertEqual(response.json()['review']['allowed_actions'], [])
        self.assertEqual(self.client.post('/api/admin/orders/a/payment', json={}).status_code, 409)
        self.trade.assert_not_called()

    def test_refund_record_is_private_manual_fact_not_gateway_call(self):
        original = self.review_job(); self.login()
        body = self.payload('record_external_refund', refund_reference='private-external-reference')
        for key in ('note', 'evidence', 'refund_reference'):
            response = self.post({**body, key: ''})
            self.assertIn(response.status_code, (409, 422), response.text)
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.trade.assert_not_called()
        self.assertFalse(self.queued)
        current = self.store.get('a')
        self.assertEqual(current.expected_amount, original.expected_amount)
        self.assertEqual(current.payment_entitlement, original.payment_entitlement)
        for secret in ('private-external-reference', body['note'], body['evidence'], 'tristan'):
            self.assertNotIn(secret, json.dumps(asdict(current), default=str, ensure_ascii=False))
        history = self.client.get('/api/admin/orders/a/review-history').json()['items']
        self.assertEqual(history[0]['refund_reference'], 'private-external-reference')

    def test_recorded_case_filters_have_correct_pagination_and_counts(self):
        self.review_job('a'); self.review_job('b'); self.review_job('c')
        self.login()
        for job_id in ('a', 'b'):
            response = self.post(self.payload(job_id=job_id), job_id)
            self.assertEqual(response.status_code, 200, response.text)
        response = self.post(self.payload('record_external_refund', job_id='b', refund_reference='record-b'), 'b')
        self.assertEqual(response.status_code, 200, response.text)
        for filter_value, total in (('paid_review', 2), ('open', 1), ('resolved', 1)):
            response = self.client.get('/api/admin/orders', params={'review': filter_value, 'size': 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['total'], total, response.text)
            self.assertEqual(len(response.json()['items']), 1)
        self.assertEqual(self.client.get('/api/admin/orders?review=not-a-filter').status_code, 422)

    def test_history_bounded_and_cursor_stable(self):
        self.review_job(); self.login()
        for i in range(3):
            response = self.post(self.payload(note=f'记录-{i}'))
            self.assertEqual(response.status_code, 200, response.text)
        first = self.client.get('/api/admin/orders/a/review-history?limit=2').json()
        self.assertEqual(len(first['items']), 2)
        self.assertTrue(first['next_cursor'])
        second = self.client.get('/api/admin/orders/a/review-history', params={'limit': 2, 'before': first['next_cursor']}).json()
        self.assertEqual(len(second['items']), 1)
        self.assertFalse({x['id'] for x in first['items']} & {x['id'] for x in second['items']})
        self.assertEqual(self.client.get('/api/admin/orders/a/review-history?limit=101').status_code, 422)
        self.assertEqual(self.client.get('/api/admin/orders/missing/review-history').status_code, 404)


if __name__ == '__main__':
    unittest.main()
