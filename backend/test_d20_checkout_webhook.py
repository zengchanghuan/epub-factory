"""Verified payment callbacks are the only public source of successful payment milestones."""
import os
import tempfile
import unittest
from unittest.mock import patch

_tmp=tempfile.TemporaryDirectory()
os.environ['DATABASE_URL']='sqlite:///'+_tmp.name+'/jobs.db'
os.environ['OPENAI_API_KEY']='dummy'
os.environ['ALIPAY_APP_ID']=''
os.environ['ALIPAY_SELLER_ID']=''
from fastapi.testclient import TestClient
from app.main import app,job_store
from app.models import Job,JobStatus,OutputMode
from app.order_events import milestones


class WebhookTests(unittest.TestCase):
    def test_validated_callback_only_and_duplicate_delivery(self):
        job_store.add(Job(id='checkout-test',source_filename='fixture.epub',trace_id='fixture',input_path='/tmp/unused',
                          output_mode=OutputMode.simplified,expected_amount='5.99',status=JobStatus.pending_payment))
        client=TestClient(app)
        data={'trade_status':'TRADE_SUCCESS','out_trade_no':'checkout-test','total_amount':'5.99'}
        with patch('app.main.verify_alipay_notification',return_value=False):
            self.assertEqual(client.post('/api/v2/webhooks/alipay',data=data).text,'fail')
        self.assertIsNone(milestones(job_store,'checkout-test')['payment_succeeded'])
        with patch('app.main.verify_alipay_notification',return_value=True), patch('app.main._use_celery',return_value=True), patch('app.tasks.job_pipeline.run_conversion.delay') as enqueue:
            self.assertEqual(client.post('/api/v2/webhooks/alipay',data={**data,'total_amount':'0.01'}).text,'fail')
            self.assertIsNone(milestones(job_store,'checkout-test')['payment_succeeded'])
            self.assertEqual(client.post('/api/v2/webhooks/alipay',data=data).text,'success')
            first=milestones(job_store,'checkout-test')['payment_succeeded']
            self.assertEqual(first['source'],'verified_webhook')
            self.assertEqual(client.post('/api/v2/webhooks/alipay',data=data).text,'success')
            self.assertEqual(milestones(job_store,'checkout-test')['payment_succeeded'],first)
            self.assertEqual(enqueue.call_count,1)
        client.close()


if __name__=='__main__':
    unittest.main()
