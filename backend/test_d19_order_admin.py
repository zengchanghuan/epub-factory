"""Offline administration regression: no live payment or model requests."""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from app.admin.auth import LoginBucket, password_hash
from app.admin.orders import cost_view
from app.admin.router import make_router
from app.domain.translation_attempt import restarted_translation_stats
from app.models import Job, JobStatus, OutputMode
from app.storage_db import Base, PersistentJobStore


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "test-password-only-123"
        cls.encoded = password_hash(cls.password)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.uploads = self.root / "uploads"; self.uploads.mkdir()
        self.outputs = self.root / "outputs"; self.outputs.mkdir()
        self.engine = create_engine(f"sqlite:///{self.root / 'jobs.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.queued = []
        self.app = FastAPI()
        self.app.include_router(make_router(self.store, self.uploads, self.outputs, lambda j, b: self.queued.append(j.id)))
        self.client = TestClient(self.app, base_url="https://testserver")
        self.env = patch.dict(os.environ, {"ADMIN_USERNAME": "tristan", "ADMIN_PASSWORD_HASH": self.encoded})
        self.env.start()
        self.payment = patch("app.admin.router.query_verified_trade")
        self.trade = self.payment.start()
        self.trade.return_value = {"out_trade_no":"a", "trade_status":"TRADE_SUCCESS", "total_amount":"5.99", "trade_no":"test"}

    def tearDown(self):
        self.client.close(); self.payment.stop(); self.env.stop(); self.engine.dispose(); self.tmp.cleanup()

    def job(self, id="a", **kwargs):
        path = self.uploads / (id + '.epub'); path.write_bytes(b'fixture')
        values = dict(id=id, source_filename="测试书.epub", output_mode=OutputMode.simplified,
                      trace_id="trace", input_path=str(path), expected_amount="5.99", status=JobStatus.failed,
                      enable_translation=True, access_token="must-not-leak", message="original failure")
        values.update(kwargs)
        job = Job(**values); self.store.add(job); return job

    def login(self, client=None):
        client = client or self.client
        r = client.post('/api/admin/login', json={"username":"tristan", "password":self.password})
        self.assertEqual(r.status_code,200,r.text)
        client.headers['X-CSRF-Token'] = r.json()['csrf']
        return r

    def retry(self, id="a"):
        return self.client.post(f'/api/admin/orders/{id}/retry', json={"acknowledge_cost":True})

    def test_private_routes_and_cookie(self):
        self.job()
        for path in ['/orders','/orders/a','/orders/a/files/source']:
            self.assertEqual(self.client.get('/api/admin'+path).status_code,401)
        r = self.login()
        for flag in ['HttpOnly', 'Secure', 'SameSite=strict']:
            self.assertIn(flag,r.headers['set-cookie'])
        text = self.client.get('/api/admin/orders').text
        self.assertNotIn('must-not-leak',text); self.assertNotIn(str(self.root),text)
        self.assertEqual(self.client.get('/api/admin/orders/a/files/source').content,b'fixture')

    def test_password_revocation_and_logout(self):
        self.login(); self.assertEqual(self.client.post('/api/admin/logout',json={}).status_code,200)
        self.assertEqual(self.client.get('/api/admin/session').status_code,401)
        self.login()
        with patch.dict(os.environ, {'ADMIN_PASSWORD_HASH':password_hash('different-password')}):
            self.assertEqual(self.client.get('/api/admin/session').status_code,401)

    def test_fail_closed_and_throttling(self):
        with patch.dict(os.environ, {'ADMIN_PASSWORD_HASH':''}):
            self.assertEqual(self.client.post('/api/admin/login',json={'username':'tristan','password':self.password}).status_code,503)
        for _ in range(10):
            self.assertEqual(self.client.post('/api/admin/login',json={'username':'tristan','password':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/api/admin/login',json={'username':'tristan','password':self.password}).status_code,429)

    def test_csrf_and_origin(self):
        self.job(); self.login()
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.retry().status_code,403)
        self.login(); self.client.headers['Origin']='https://attacker.invalid'
        self.assertEqual(self.retry().status_code,403); self.assertFalse(self.queued)
        self.assertEqual(self.client.post('/api/admin/login',json={'username':'tristan','password':self.password}).status_code,403)

    def test_unknown_mismatch_closed_never_retry(self):
        self.job(); self.login()
        for reply in [None, {'trade_status':'TRADE_SUCCESS','total_amount':'0.01'},
                      {'trade_status':'TRADE_CLOSED','total_amount':'5.99'},
                      {'trade_status':'TRADE_SUCCESS','total_amount':'NaN'}]:
            self.trade.return_value=reply
            self.assertEqual(self.retry().status_code,409)
            self.assertEqual(self.store.get('a').status,JobStatus.failed)
        self.assertFalse(self.queued)

    def test_paid_retry_once_preserves_cost(self):
        self.job(translation_stats={'prompt_tokens':100,'completion_tokens':20,'cost_usd':0.1,'translation_attempt':2})
        self.login(); self.assertEqual(self.retry().status_code,200)
        self.assertEqual(self.retry().status_code,409); self.assertEqual(self.queued,['a'])
        job=self.store.get('a'); self.assertEqual(job.cache_policy,'reuse')
        self.assertEqual(cost_view(job.translation_stats)['estimated_usd'],'0.1')
        self.assertEqual(self.store.list_stages('a')[0].metadata['previous_message'],'original failure')

    def test_only_failed_and_source_containment(self):
        self.job(status=JobStatus.success); self.login()
        self.assertEqual(self.retry().status_code,409)
        outside=self.root/'private.env'; outside.write_text('secret')
        self.job('b',input_path=str(outside))
        self.assertEqual(self.client.get('/api/admin/orders/b/files/source').status_code,410)
        self.assertEqual(self.retry('b').status_code,410)
        link=self.uploads/'link'; link.symlink_to(outside)
        self.job('c',input_path=str(link))
        self.assertEqual(self.client.get('/api/admin/orders/c/files/source').status_code,410)

    def test_search_amount_dates_and_batch(self):
        self.job(created_at=datetime(2026,9,1,tzinfo=timezone.utc))
        self.job('b',source_filename='another',expected_amount='38.98')
        self.job('c',batch_id='batch1',batch_index=0,expected_amount='19.99')
        self.job('d',batch_id='batch1',batch_index=1,expected_amount='')
        self.login()
        for query,count in [('q=测试书',3),('q=%25',0),('amount=5.99',1),('amount=19.99',2),('start=2026-09-01&end=2026-09-01',1),('payment=paid',0)]:
            r=self.client.get('/api/admin/orders?'+query);self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['total'],count)
        self.assertEqual(self.client.get('/api/admin/orders?amount=NaN').status_code,422)
        self.assertEqual(self.client.get('/api/admin/orders?start=2026-09-02&end=2026-09-01').status_code,422)
        self.trade.return_value={'trade_status':'TRADE_SUCCESS','total_amount':'19.99'}
        self.assertEqual(self.retry('d').status_code,200);self.trade.assert_called_with('batch_batch1')

    def test_compare_and_swap_parallel_claims(self):
        self.job()
        def claim(i):
            return self.store.restart_translation_attempt('a',attempt_id=str(i),action_label='test',max_free_retries=-1,
                    started_at=datetime.now(timezone.utc),failed_only=True)[1]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(claim, range(2)))
        self.assertEqual(results.count('ok'),1)

    def test_stale_admin_request_cannot_claim_new_failure(self):
        original=self.job()
        self.store.update_status('a', JobStatus.failed, message='a newer attempt failed')
        result=self.store.restart_translation_attempt('a',attempt_id='stale',action_label='test',max_free_retries=-1,
                started_at=datetime.now(timezone.utc),failed_only=True,expected_updated_at=original.updated_at)
        self.assertEqual(result[1],'active')

    def test_retry_acknowledgement_and_enqueue_failure(self):
        self.job(); self.login()
        self.assertEqual(self.client.post('/api/admin/orders/a/retry',json={}).status_code,422)
        app=FastAPI()
        def fail(job, background):
            raise RuntimeError('queue unavailable')
        app.include_router(make_router(self.store,self.uploads,self.outputs,fail))
        with TestClient(app,base_url='https://testserver') as client:
            self.login(client)
            r=client.post('/api/admin/orders/a/retry',json={'acknowledge_cost':True})
            self.assertEqual(r.status_code,503,r.text)
            self.assertEqual(self.store.get('a').status,JobStatus.failed)
            self.assertIn('入队失败',self.store.get('a').message)

    def test_non_translation_failed_order_can_retry(self):
        self.job(enable_translation=False);self.login()
        self.assertEqual(self.retry().status_code,200)
        self.assertEqual(self.queued,['a'])

    def test_unknown_cost_not_zero(self):
        for stats in [{},{'cost_usd':0,'prompt_tokens':0},{'cost_usd':'NaN','prompt_tokens':10}]:
            self.assertIsNone(cost_view(stats)['estimated_usd'])
        old={'cost_usd':0.2,'prompt_tokens':100,'translation_attempt':31}
        new=restarted_translation_stats(old,attempt_id='new')
        self.assertEqual(cost_view(new)['known_attempts'],1)
        self.assertEqual(cost_view(new)['attempts'],32)

    def test_password_setup_preserves_config_and_hides_secret(self):
        import importlib.util
        import sys
        from contextlib import redirect_stdout
        from io import StringIO
        script=Path(__file__).resolve().parents[1]/'scripts/setup-admin.py'
        spec=importlib.util.spec_from_file_location('setup_admin_test',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        config=self.root/'.env'
        config.write_text('DATABASE_URL=sqlite:///existing.db\nADMIN_USERNAME=old\nADMIN_PASSWORD_HASH=old\n')
        output=StringIO()
        with patch.object(sys,'argv',['setup-admin.py','--env',str(config)]), patch.object(module.os,'isatty',return_value=True), \
             patch.object(module.getpass,'getpass',side_effect=[self.password,self.password]), redirect_stdout(output):
            module.main()
        result=config.read_text()
        self.assertIn('DATABASE_URL=sqlite:///existing.db',result)
        self.assertIn('ADMIN_USERNAME=tristan',result)
        self.assertEqual(result.count('ADMIN_PASSWORD_HASH='),1)
        self.assertNotIn(self.password,result+output.getvalue())
        self.assertEqual(config.stat().st_mode & 0o777,0o600)
        from app.admin.auth import verify_password
        encoded=result.split('ADMIN_PASSWORD_HASH=')[1].strip().strip("'")
        self.assertTrue(verify_password(self.password,encoded))

    def test_checkout_auth_idempotence_and_no_client_payment_success(self):
        from app.order_events import make_event_router, milestones, record_event
        self.job()
        self.app.include_router(make_event_router(self.store,lambda req,j:req.headers.get('X-Job-Token')==j.access_token))
        url='/api/v2/jobs/a/checkout-events'
        self.assertEqual(self.client.post(url,json={'event':'quote_shown'}).status_code,403)
        headers={'X-Job-Token':'must-not-leak'}
        self.assertEqual(self.client.post(url,json={'event':'payment_succeeded'},headers=headers).status_code,422)
        self.assertEqual(self.client.post(url,json={'event':'quote_shown'},headers=headers).status_code,200)
        first=milestones(self.store,'a')['quote_shown']
        self.client.post(url,json={'event':'quote_shown'},headers=headers)
        self.assertEqual(milestones(self.store,'a')['quote_shown'],first)
        self.client.post(url,json={'event':'payment_clicked'},headers=headers)
        self.assertIsNone(milestones(self.store,'a')['payment_succeeded'])
        self.assertEqual(self.store.get('a').status,JobStatus.failed)
        self.login()
        result=self.client.get('/api/admin/orders/a').json()
        self.assertIsNotNone(result['checkout']['payment_clicked'])
        self.client.post('/api/admin/orders/a/payment',json={})
        self.assertEqual(milestones(self.store,'a')['payment_succeeded']['source'],'verified_query')

    def test_batch_checkout_shares_order_and_history_stays_unknown(self):
        from app.order_events import make_event_router,milestones
        self.job('a',batch_id='b1',batch_index=0)
        self.job('b',batch_id='b1',batch_index=1)
        self.app.include_router(make_event_router(self.store,lambda req,j:True))
        self.assertTrue(all(v is None for v in milestones(self.store,'batch_b1').values()))
        self.client.post('/api/v2/jobs/b/checkout-events',json={'event':'quote_shown'})
        self.login()
        a=self.client.get('/api/admin/orders/a').json()['checkout']
        b=self.client.get('/api/admin/orders/b').json()['checkout']
        self.assertEqual(a,b)
        self.assertIsNotNone(a['quote_shown'])

    def test_test_order_marker_excluded_at_any_price_and_survives_retry(self):
        self.job('real')
        self.job('test',is_test_order=True,expected_amount='79.59')
        self.job('head',is_test_order=True,batch_id='test-batch',batch_index=0,expected_amount='99.99')
        self.job('child',is_test_order=True,batch_id='test-batch',batch_index=1,expected_amount='')
        self.login()
        result=self.client.get('/api/admin/orders').json()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['id'],'real')
        self.store.restart_translation_attempt('test',attempt_id='new',action_label='test',max_free_retries=-1,
                started_at=datetime.now(timezone.utc))
        self.assertTrue(self.store.get('test').is_test_order)

    def test_verified_sdk_shapes_and_reject_exception(self):
        from app.infra import alipay
        with patch.object(alipay,'_alipay_client') as sdk:
            import json
            payload={'code':'10000','out_trade_no':'a','trade_status':'TRADE_SUCCESS','total_amount':'5.99'}
            for response in [payload,{'alipay_trade_query_response':payload}]:
                sdk.execute.return_value=json.dumps(response)
                self.assertEqual(alipay.query_verified_trade('a')['total_amount'],'5.99')
            self.assertIsNone(alipay.query_verified_trade('different'))
            sdk.execute.side_effect=Exception(json.dumps({'alipay_trade_query_response':payload}))
            self.assertIsNone(alipay.query_verified_trade('a'))


if __name__ == '__main__':
    unittest.main()
