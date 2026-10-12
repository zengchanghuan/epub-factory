"""Repeatable offline OCR fault/soak workload; not a live uptime claim."""
import json
import os
import random
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.domain.pdf import config, providers, state
from app.domain.pdf.concurrency import ordered_pages
from test_d37_pdf_pipeline import response


class FaultSoakTests(unittest.TestCase):
    def test_sync_glossary_does_not_leak_event_loops_across_jobs(self):
        import asyncio
        from unittest.mock import AsyncMock
        from app.engine import glossary_service
        asyncio.set_event_loop(None)
        try:
            with patch.object(glossary_service,'build_consistent_glossary_async',new=AsyncMock(return_value='ok')):
                for _ in range(10):
                    self.assertEqual(glossary_service.build_consistent_glossary([]),'ok')
                    with self.assertRaises(RuntimeError): asyncio.get_event_loop()
        finally:
            try: loop=asyncio.get_event_loop()
            except RuntimeError: pass
            else: loop.close();asyncio.set_event_loop(None)

    def test_killed_worker_with_unknown_ocr_response_is_not_recharged(self):
        import subprocess,sys
        code = """
import sys,time
from pathlib import Path
from unittest.mock import patch
from app.domain.pdf.providers import Flash
root=Path(sys.argv[1]);image=root/'image.jpg';image.write_bytes(b'offline-fixture')
def block(_):
    (root/'inflight').write_text('ready')
    while True:time.sleep(1)
with patch.object(Flash,'call',side_effect=block):Flash(root).extract(image,1)
"""
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);child=subprocess.Popen([sys.executable,'-c',code,d],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+10
                while not (root/'inflight').exists():
                    self.assertIsNone(child.poll())
                    self.assertLess(time.monotonic(),deadline)
                    time.sleep(.01)
                child.kill();child.wait(timeout=5)
            finally:
                if child.poll() is None:child.kill();child.wait(timeout=5)
            with patch.object(providers.Flash,'call',side_effect=AssertionError('duplicate paid call')):
                with self.assertRaisesRegex(config.PdfError,'待核实'):
                    providers.Flash(root).extract(root/'image.jpg',1)
            self.assertEqual(state.read(root/'flash-budget.json')['requests'],1)
            self.assertEqual(state.read(root/'flash-1-full.json')['state'],'pending')

    def test_duplicate_concurrent_page_is_paid_once(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);img=root/'i.jpg';img.write_bytes(b'synthetic-image')
            flash=providers.Flash(root)
            with patch.object(flash,'call',return_value=response()) as call, ThreadPoolExecutor(max_workers=8) as pool:
                values=list(pool.map(lambda _:flash.extract(img,1),range(40)))
            self.assertEqual(call.call_count,1);self.assertEqual(state.read(root/'flash-budget.json')['requests'],1)
            self.assertEqual(len(values),40)

    def test_real_sdk_thread_accounting_is_complete_even_for_bad_json(self):
        import httpx
        from openai import OpenAI
        from sqlalchemy import create_engine
        from app.infra.llm_usage_ledger import usage_scope,get_ledger
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'PDF_DEEPSEEK_API_KEY':'offline'}):
            root=Path(d);img=root/'i.jpg';img.write_bytes(b'fixture')
            engine=create_engine('sqlite:///'+str(root/'usage.db'))
            lock=threading.Lock(); count=0
            def handler(request):
                nonlocal count
                with lock: count+=1; i=count
                return httpx.Response(200,json={'id':f'offline-{i}','object':'chat.completion','created':1,'model':'deepseek-flash',
                    'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'invalid' if i%3==0 else '{"uncertain":false,"blocks":[{"text":"source"}]}'}}],
                    'usage':{'prompt_tokens':100,'completion_tokens':30,'total_tokens':130,'prompt_cache_hit_tokens':80,'prompt_cache_miss_tokens':20}})
            def factory(**kwargs):
                kwargs.pop('http_client').close()
                return OpenAI(**kwargs,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            flash=providers.Flash(root,factory)
            def work(i,check):
                try:return flash.extract(img,i,check)
                except config.PageReviewRequired:return None
            try:
                with usage_scope('parallel','first',engine=engine):
                    values=list(ordered_pages(range(1,13),work,workers=4))
                self.assertEqual(sum(x is None for x in values),4)
                ledger=get_ledger(engine);summary=ledger.summary('parallel')
                self.assertEqual(summary['coverage'],'complete'); self.assertEqual(summary['prompt_tokens']+summary['completion_tokens'],12*130)
                self.assertEqual({r['stage'] for r in ledger.requests('parallel')},{'pdf_ocr'})
                with usage_scope('parallel','resume',engine=engine):
                    list(ordered_pages(range(1,13),work,workers=4))
                self.assertEqual(count,12);self.assertEqual(state.read(root/'flash-budget.json')['requests'],12)
            finally:engine.dispose()

    def test_repeated_faults_and_restarts_have_no_duplicate_or_lost_requests(self):
        # Fixed seed and bounded random completion order make this reproducible.
        cycles=int(os.environ.get('PDF_SOAK_CYCLES','25'));rng=random.Random(772)
        for cycle in range(cycles):
            with self.subTest(cycle=cycle), tempfile.TemporaryDirectory() as d:
                root=Path(d);flash=providers.Flash(root)
                pages=list(range(1,13));delays={p:rng.random()/1000 for p in pages}
                for p in pages:(root/f'{p}.jpg').write_bytes(str(p).encode())
                called=[];lock=threading.Lock()
                def call(data):
                    p=int(data)
                    with lock:called.append(p)
                    time.sleep(delays[p])
                    if p%5==0:raise TimeoutError('result unknown')
                    if p%4==0:return response({'blocks':[{'text':'missing confidence'}]})
                    return response()
                def work(p,check):
                    try: return flash.extract(root/f'{p}.jpg',p,check)
                    except config.PageReviewRequired:return None
                with patch.object(flash,'call',side_effect=call):
                    with self.assertRaises(config.PdfError):list(ordered_pages(pages,work,workers=4))
                    before=list(called)
                    with self.assertRaises(config.PdfError):list(ordered_pages(pages,work,workers=4))
                self.assertEqual(called,before)
                self.assertEqual(len(called),len(set(called)))
                self.assertEqual(state.read(root/'flash-budget.json')['requests'],len(called))
                for p in called:
                    checkpoint=state.read(root/f'flash-{p}-full.json')
                    self.assertIn(checkpoint['state'],{'done','invalid','pending'})
                    if p%5==0:self.assertEqual(checkpoint['state'],'pending')

if __name__=='__main__':unittest.main()
