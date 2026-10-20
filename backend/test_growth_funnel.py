"""Synthetic only: analytics cannot grant entitlement or initiate payments."""
import os
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from fastapi import FastAPI
from app import growth
from app.order_events import record_event, make_event_router
from app.storage_db import PayPalOrderRecord
import test_d19_order_admin as admin_fixtures

class FunnelTests(unittest.TestCase):
    setUpClass=classmethod(admin_fixtures.AdminTests.setUpClass.__func__)
    job=admin_fixtures.AdminTests.job
    login=admin_fixtures.AdminTests.login
    def setUp(self):
        admin_fixtures.AdminTests.setUp(self)
        self.analytics=patch.dict(os.environ,{'FUNNEL_DB_PATH':str(self.root/'funnel.sqlite')});self.analytics.start()
        self.app.include_router(growth.router())
        self.app.include_router(make_event_router(self.store,lambda request,job:request.headers.get('X-Job-Token')==job.access_token))
    def tearDown(self):
        self.analytics.stop();admin_fixtures.AdminTests.tearDown(self)
    def visit(self,sid='a'*32,**kwargs):
        return self.client.post('/api/v2/analytics/events',json=dict(session=sid,event='visit',language='en',device='mobile',source='search',**kwargs))
    def linked(self,id,**kwargs):
        job=self.job(id,**kwargs);growth.bind(id,'a'*32);return job
    def test_server_paid_fact_dedup_and_test_sandbox_exclusion(self):
        self.visit();self.visit()
        self.client.post('/api/v2/analytics/events',json={'session':'a'*32,'event':'file_selected'})
        for id,options in [('paid',{}),('unpaid',{}),('test',{'is_test_order':True}),('sandbox',{})]:
            job=self.linked(id,**options)
            record_event(self.store,id,'quote_shown','browser');record_event(self.store,id,'payment_clicked','browser')
            record_event(self.store,id,'payment_succeeded','browser' if id=='unpaid' else 'verified_webhook')
            growth.downloaded(job)
        with self.store._Session() as db:
            db.add(PayPalOrderRecord(local_id='sandbox',order_id='provider',merchant_id='merchant',amount='1.49',mode='sandbox',created_at=datetime.now(timezone.utc)));db.commit()
        r=growth.report(self.store)
        self.assertEqual(r['counts'],{'visits':1,'selected':1,'quoted':2,'clicked':2,'paid':1,'downloaded':1})
        self.assertEqual(r['quote_to_paid'],.5);self.assertEqual(r['paid_to_download'],1)
        self.assertEqual(growth.report(self.store,language='ja')['counts']['quoted'],0)
        self.assertEqual(growth.report(self.store,source='search',device='mobile')['counts']['paid'],1)
        self.trade.assert_not_called();self.assertFalse(self.queued)
    def test_batch_repair_pdf_are_separate_orders(self):
        self.visit()
        for id in ('child1','child2'):
            self.job(id,batch_id='b')
        self.job('pdf',output_mode=admin_fixtures.OutputMode.original,enable_translation=False)
        for number in ('batch_b','pdf','repair_r','repair_test'):
            growth.bind(number,'a'*32)
            record_event(self.store,number,'quote_shown','browser')
            record_event(self.store,number,'payment_succeeded','verified_query')
            growth.downloaded_order(number)
        repairs={'r':{'is_test_order':False},'test':{'is_test_order':True}}
        r=growth.report(self.store,repair_lookup=repairs.get)
        self.assertEqual(r['counts']['quoted'],3)
        self.assertEqual(r['counts']['paid'],3)
        self.assertEqual(r['counts']['downloaded'],3)
        for key in ('batch','pdf','repair'):
            self.assertEqual(r['services'][key],{'quoted':1,'paid':1})
        self.trade.assert_not_called();self.assertFalse(self.queued)

    def test_expired_sessions_are_removed_and_first_attribution_wins(self):
        self.visit();self.visit('b'*32)
        self.linked('bound')
        growth.bind('bound','b'*32)
        record_event(self.store,'bound','quote_shown','browser')
        import sqlite3,time
        with sqlite3.connect(growth.path()) as db:
            self.assertEqual(db.execute('SELECT session FROM bindings').fetchone()[0],'a'*32)
            db.execute('UPDATE visits SET started=? WHERE session=?',(time.time()-91*86400,'a'*32))
        self.visit('c'*32)
        r=growth.report(self.store)
        self.assertEqual(r['counts']['visits'],2);self.assertEqual(r['counts']['quoted'],0)

    def test_private_readonly_report_no_secrets_or_zero_rate(self):
        self.assertEqual(self.client.get('/api/admin/funnel').status_code,401)
        self.login();r=self.client.get('/api/admin/funnel');self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.headers['cache-control'],'no-store');self.assertIsNone(r.json()['quote_to_paid'])
        self.assertNotIn('must-not-leak',r.text)
        for q in ['days=0','days=91','language=bad','source=anything']:
            self.assertEqual(self.client.get('/api/admin/funnel?'+q).status_code,422)
        self.trade.assert_not_called()
    def test_browser_cannot_submit_payment_or_private_content(self):
        for data in [{'session':'a'*32,'event':'payment_succeeded'},{'session':'a'*32,'event':'visit','book':'secret'},{'session':'url-or-token','event':'visit'}]:
            self.assertEqual(self.client.post('/api/v2/analytics/events',json=data).status_code,422)
        for headers in [{'user-agent':'Googlebot'},{'DNT':'1'}]:
            self.assertFalse(self.client.post('/api/v2/analytics/events',json={'session':'a'*32,'event':'visit'},headers=headers).json()['recorded'])
        self.assertEqual(growth.report(self.store)['counts']['visits'],0)
    def test_binding_requires_job_authorization(self):
        self.visit();job=self.job('bound')
        payload={'event':'quote_shown','funnel_session':'a'*32}
        self.assertEqual(self.client.post('/api/v2/jobs/bound/checkout-events',json=payload).status_code,403)
        r=self.client.post('/api/v2/jobs/bound/checkout-events',json=payload,headers={'X-Job-Token':job.access_token})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(growth.report(self.store)['counts']['quoted'],1)
    def test_telemetry_failure_does_not_break_checkout_event(self):
        job=self.job('bound')
        with patch('app.growth.sqlite3.connect',side_effect=OSError('unavailable')):
            r=self.client.post('/api/v2/jobs/bound/checkout-events',json={'event':'quote_shown','funnel_session':'a'*32},headers={'X-Job-Token':job.access_token})
            self.assertEqual(r.status_code,200,r.text)
            growth.downloaded(job)
        self.trade.assert_not_called()

if __name__=='__main__':unittest.main()
