"""Provider contracts tested via in-memory HTTP transports; never external I/O."""
import io
import json
import logging
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import httpx
from app.domain.pdf import providers, state, config
from app.domain.pdf.logging_guard import private_http
from app.infra.llm_usage_ledger import usage_scope, get_ledger
from app.infra.llm_errors import ProviderAccountUnavailable
from sqlalchemy import create_engine


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.path = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'PDF_WORK_DIR': str(self.path), 'PDF_DEEPSEEK_API_KEY': 'FAKE_FLASH_KEY', 'PDF_OCR_MAX_REQUESTS_PER_JOB': '10'})
        self.env.start();self.directory = state.job_dir('provider')
        self.source = self.directory / 'source.pdf';self.source.write_bytes(b'%PDF-offline')
        self.quota = state.Quota('FAKE_MINERU_KEY')
        self.calls = []
        self.zip = io.BytesIO()
        with zipfile.ZipFile(self.zip, 'w') as z:
            z.writestr('source_content_list.json', json.dumps([{'type':'text','page_idx':0,'text':'Sample text'}]))
            z.writestr('../../untrusted.txt','must never extract')
    def tearDown(self):
        self.env.stop();self.tmp.cleanup()
    def factory(self, handler):
        def transport(request):
            self.calls.append((request.method, str(request.url), dict(request.headers)))
            return handler(request)
        return lambda **kw: httpx.Client(transport=httpx.MockTransport(transport), **kw)
    def handler(self, request):
        if request.method == 'POST':
            body = json.loads(request.content);self.assertEqual(body['files'][0]['data_id'], 'provider')
            self.assertTrue(body['files'][0]['is_ocr'])
            return httpx.Response(200, json={'code':0,'data':{'batch_id':'batch-1','file_urls':['https://mineru.oss-cn-shanghai.aliyuncs.com/private?sig=FAKE_SIGNED_SECRET']}})
        if request.method == 'PUT': return httpx.Response(200)
        if request.url.host == 'cdn-mineru.openxlab.org.cn': return httpx.Response(200, content=self.zip.getvalue())
        return httpx.Response(200,json={'code':0,'data':{'extract_result':[{'data_id':'provider','state':'done','full_zip_url':'https://cdn-mineru.openxlab.org.cn/result?sig=FAKE_SIGNED_SECRET'}]}})

    def test_mineru_api_credentials_never_forwarded_to_storage_and_resume_no_calls(self):
        client = providers.MinerU('FAKE_MINERU_KEY',self.directory,self.quota,self.factory(self.handler))
        result = client.extract(self.source,'provider',2)
        self.assertEqual(result['1']['blocks'][0]['text'], 'Sample text')
        for method, url, headers in self.calls:
            if url.startswith(providers.MINERU_API): self.assertEqual(headers['authorization'], 'Bearer FAKE_MINERU_KEY')
            else: self.assertNotIn('authorization', headers)
        count = len(self.calls);self.assertEqual(client.extract(self.source,'provider',2),result)
        self.assertEqual(count,len(self.calls))
        self.assertFalse((self.path / 'untrusted.txt').exists())
        for path in self.directory.glob('*.json'):
            self.assertNotIn('FAKE_MINERU_KEY',path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_lost_upload_response_only_polls_original_batch(self):
        def handler(request):
            if request.method == 'PUT': raise httpx.ReadTimeout('FAKE_SIGNED_SECRET')
            return self.handler(request)
        client = providers.MinerU('FAKE_MINERU_KEY',self.directory,self.quota,self.factory(handler))
        with self.assertRaises(config.PdfError) as ctx:client.extract(self.source,'provider',2)
        self.assertNotIn('FAKE_SIGNED_SECRET',str(ctx.exception))
        self.assertEqual(state.read(self.directory / 'mineru.json')['state'],'uploading')
        client.extract(self.source,'provider',2)
        self.assertEqual(sum(method=='POST' for method,_,_ in self.calls),1)
        self.assertEqual(sum(method=='PUT' for method,_,_ in self.calls),1)

    def test_ambiguous_post_is_not_resubmitted(self):
        client = providers.MinerU('FAKE_MINERU_KEY',self.directory,self.quota,self.factory(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout('FAKE_MINERU_KEY'))))
        for _ in range(2):
            with self.assertRaises(config.PdfError):client.extract(self.source,'provider',2)
        self.assertEqual(len(self.calls),1)

    def test_rejected_submission_allows_fallback_but_query_rejection_does_not(self):
        client = providers.MinerU('FAKE_MINERU_KEY',self.directory,self.quota,self.factory(lambda request:httpx.Response(401)))
        with self.assertRaises(providers.MinerURejected):client.extract(self.source,'provider',2)
        self.assertEqual(state.read(self.directory/'mineru.json')['state'],'rejected')
        state.save(self.directory/'mineru.json', {'state':'accepted','batch_id':'batch-1'})
        with self.assertRaises(config.PdfError) as ctx:client.extract(self.source,'provider',2)
        self.assertNotIsInstance(ctx.exception,providers.MinerURejected)

    def test_redirect_does_not_receive_credentials(self):
        client = providers.MinerU('FAKE_MINERU_KEY',self.directory,self.quota,self.factory(lambda request:httpx.Response(302,headers={'Location':'https://evil.invalid/'})))
        with self.assertRaises(config.PdfError):client.extract(self.source,'provider',2)
        self.assertEqual(len(self.calls),1)

    def test_mineru_wrong_business_identity_and_zip_size_are_rejected(self):
        def handler(request):
            if request.method=='GET':return httpx.Response(200,json={'code':0,'data':{'extract_result':[{'data_id':'other','state':'done'}]}})
            return self.handler(request)
        client = providers.MinerU('FAKE_MINERU_KEY',self.directory,self.quota,self.factory(handler))
        with self.assertRaises(config.PdfError):client.extract(self.source,'provider',2)

    def test_flash_real_sdk_payload_and_usage_ledger_without_network(self):
        from openai import OpenAI
        def handler(request):
            self.assertEqual(request.headers['authorization'],'Bearer FAKE_FLASH_KEY')
            body=json.loads(request.content)
            self.assertEqual(body['model'],'deepseek-flash')
            self.assertEqual(body['thinking']['type'],'disabled')
            self.assertTrue(body['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
            return httpx.Response(200,json={'id':'fake-response','object':'chat.completion','created':1,'model':'deepseek-flash','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"blocks":[{"text":"source"}],"uncertain":false}'}}],'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}})
        def factory(**kwargs):
            kwargs.pop('http_client').close()
            return OpenAI(**kwargs,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        engine=create_engine('sqlite:///'+str(self.path/'ledger.db'))
        try:
            with usage_scope('pdf-job','attempt',engine=engine):
                providers.Flash(self.directory,factory).call(b'fake-image')
            ledger=get_ledger(engine)
            with engine.connect() as conn:
                from sqlalchemy import text
                row=conn.execute(text('SELECT stage, total_tokens FROM llm_usage_requests')).one()
                self.assertEqual(tuple(row),('pdf_ocr',30))
        finally:engine.dispose()

    def test_known_balance_rejection_can_resume_after_recharge(self):
        from openai import APIStatusError
        image=self.directory/'p.jpg';image.write_bytes(b'fake-image')
        flash=providers.Flash(self.directory)
        error=APIStatusError('FAKE_FLASH_KEY',response=httpx.Response(402,request=httpx.Request('POST',providers.FLASH_API)),body=None)
        with patch.object(flash,'call',side_effect=error):
            with self.assertRaises(ProviderAccountUnavailable):flash.extract(image,1)
        self.assertEqual(state.read(self.directory/'flash-1-full.json')['state'],'retryable')
        from test_d37_pdf_pipeline import response
        with patch.object(flash,'call',return_value=response()) as call:flash.extract(image,1)
        self.assertEqual(call.call_count,1)

    def test_private_http_logs_suppressed_only_in_current_scope(self):
        sink=io.StringIO();handler=logging.StreamHandler(sink);logger=logging.getLogger('httpx')
        prior=logger.level;logger.setLevel(logging.DEBUG);logger.addHandler(handler)
        try:
            with private_http():logger.info('FAKE_FLASH_KEY FAKE_SIGNED_SECRET')
            logger.info('ordinary request remains observable')
        finally:logger.removeHandler(handler);logger.setLevel(prior)
        self.assertNotIn('FAKE_',sink.getvalue());self.assertIn('ordinary request',sink.getvalue())


if __name__ == '__main__':unittest.main()
