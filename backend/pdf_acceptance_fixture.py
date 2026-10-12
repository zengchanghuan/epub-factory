"""Offline acceptance harness: real pipeline/SDK/ledger, synthetic HTTP responses.

Runs in a separate process so tests can SIGKILL a worker after a durable chunk.
Never uses live credentials, network, customer orders or production databases.
"""
import json
import os
import socket
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

TEXTS = ['Alpha proposes this approach.', 'Beta supports this approach.', 'Gamma checks this approach.']
TRANSLATIONS = ['阿尔法提出这种方法。', '贝塔支持这种方法。', '伽马检查这种方法。']


def run(root, pause=False):
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    env={
        'PDF_WORK_DIR':str(root/'pdf'), 'PDF_TRANSLATION_ENABLED':'1',
        'PDF_TRANSLATION_PRICE_PER_PAGE_CNY':'.20', 'PDF_DEEPSEEK_API_KEY':'offline',
        'MINERU_API_KEY':'', 'PDF_OCR_CONCURRENCY':'2',
        'OPENAI_API_KEY':'offline', 'OPENAI_BASE_URL':'https://api.deepseek.com/v1',
        'OPENAI_BASE_URL_FALLBACKS':'', 'OPENAI_MODEL':'deepseek-flash',
        'EPUB_DEFAULT_TRANSLATION_MODEL':'deepseek-flash',
        'EPUB_TRANSLATION_CHECKPOINT_DB':str(root/'checkpoints.db'),
        'EPUB_BOOK_PROFILER_ENABLED':'0', 'EPUB_LLM_RATE_LIMITER_ENABLED':'0',
        'EPUB_LLM_GLOBAL_HEALTH_ENABLED':'0', 'EPUB_CHAPTER_CONCURRENCY_CAP':'1',
        'OPENAI_CONCURRENCY':'1', 'EPUB_TRANSLATION_QUALITY_RETRIES':'1',
        'CELERY_BROKER_URL':'', 'REDIS_URL':'',
    }
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,env))
        stack.enter_context(patch.object(socket.socket,'connect',side_effect=AssertionError('offline acceptance forbids network')))
        from datetime import datetime, timezone
        stack.enter_context(patch('app.infra.llm_usage_ledger.now', return_value=datetime(2026,9,18,2,tzinfo=timezone.utc)))
        import httpx
        from openai import AsyncOpenAI
        from sqlalchemy import create_engine
        from app.domain.pdf import pipeline, providers, state
        from app.domain.fast_translation_runner import run_fast_translation_job
        from app.domain.translation_checkpoints import TranslationCheckpoints
        from app.engine.translation_cache import TranslationCache
        from app.infra.llm_usage_ledger import usage_scope, get_ledger
        from app.models import Job, JobStatus, OutputMode
        from app.storage import JobStore
        from test_d37_pdf_pipeline import fixture, response
        source=root/'fixture.pdf'
        if not source.exists(): fixture(source,('scan',)*3)
        pfpath=root/'preflight.json'
        if not pfpath.exists():
            pf=pipeline.preflight(source,'acceptance');pf['confirmed']=True
            state.save(pfpath,pf)
        pf=state.read(pfpath)
        engine=create_engine('sqlite:///'+str(root/'ledger.db'))
        def ocr(self,data):
            # Render fixtures are identical; page identity is supplied via the
            # extract wrapper below while retaining real durable Flash.call logic.
            from app.infra.llm_usage_ledger import accounted_call
            from test_d31_llm_usage_ledger import response as billed
            from app.domain.pdf.providers import FLASH_API, MODEL
            value=response({'uncertain':False,'blank':False,'blocks':[{'type':'text','text':TEXTS[0]}]})
            value.usage=billed().usage; value.model=MODEL; value.id='offline-ocr-'+uuid.uuid4().hex
            return accounted_call(lambda:value,model=MODEL,base_url=FLASH_API,stage='pdf_ocr')
        original_extract=providers.Flash.extract
        def extract(self,image,page,cancel=lambda:None,**kwargs):
            result=original_extract(self,image,page,cancel,**kwargs)
            result['blocks']=[{'type':'text','text':TEXTS[page-1]}]
            if page==1:
                result['blocks'] += [{'type':'code','text':'if x < 2:\n    y = 3'}, {'type':'equation','text':r'\frac{1}{60}'}]
            return result
        stack.enter_context(patch.object(providers.Flash,'call',ocr))
        stack.enter_context(patch.object(providers.Flash,'extract',extract))
        def transport(request):
            body=json.loads(request.content)
            user=body['messages'][-1]['content']
            with (root/'requests.jsonl').open('a') as stream:
                stream.write(json.dumps({'input':user},ensure_ascii=False)+'\n');stream.flush();os.fsync(stream.fileno())
            if '候选术语：' in user:
                items=json.loads(user.split('候选术语：',1)[1])
                mapping={'Alpha':'阿尔法','Beta':'贝塔','Gamma':'伽马'}
                result={'translations':{i['term']:mapping.get(i['term'],'术语') for i in items}}
            else:
                items=json.loads(user);results=[]
                for item in items:
                    text=item['html']
                    for a,b in zip(TEXTS,TRANSLATIONS): text=text.replace(a,b)
                    text=text.replace('fixture','离线验收')
                    results.append({'id':item['id'],'translation':text})
                result={'results':results}
            return httpx.Response(200,json={
                'id':'offline-'+uuid.uuid4().hex, 'object':'chat.completion','created':1,'model':'deepseek-flash',
                'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(result,ensure_ascii=False)},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':100,'completion_tokens':30,'total_tokens':130,'prompt_cache_hit_tokens':80,'prompt_cache_miss_tokens':20}})
        def client(**kwargs):
            return AsyncOpenAI(api_key='offline',base_url='https://api.deepseek.com/v1',max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        stack.enter_context(patch('app.engine.cleaners.semantics_translator.AsyncOpenAI',side_effect=client))
        stack.enter_context(patch('openai.AsyncOpenAI',side_effect=client))
        stack.enter_context(patch('app.engine.cleaners.semantics_translator.TranslationCache',side_effect=lambda:TranslationCache(str(root/'cache.db'))))
        store=JobStore(); stack.enter_context(patch('app.domain.fast_translation_runner.job_store',store))
        job=Job(id='acceptance',trace_id='offline',source_filename='fixture.pdf',input_path=str(source),
                output_mode=OutputMode.simplified,enable_translation=True,status=JobStatus.running,
                translation_stats={'attempt_id':'resume' if not pause else 'interrupted','translation_preflight':pf})
        store.add(job)
        original_put=TranslationCheckpoints.put
        def put(self,key,payload):
            original_put(self,key,payload)
            if pause and key.startswith('chunk:') and payload.get('phase')=='final':
                state.save(root/'paused.json',{'key':key})
                while True: time.sleep(1)
        stack.enter_context(patch.object(TranslationCheckpoints,'put',put))
        with usage_scope(job.id,job.translation_stats['attempt_id'],engine=engine):
            epub,summary=pipeline.prepare(source,job.id,pf['pdf'])
            result=run_fast_translation_job(job=job,input_path=epub,output_path=root/'translated.epub',
                    progress_callback=lambda _:None,stage_callback=lambda *args:None)
            from app.job_runner import _apply_final_artifact_audit
            _apply_final_artifact_audit(job,result,root/'translated.epub')
            pipeline.apply_review_gate(result,summary)
        report={'passed':result.validation_passed,'message':result.message,'stats':result.translation_stats,
                'usage':get_ledger(engine).summary(job.id),'pdf':summary}
        state.save(root/'report.json',report)
        engine.dispose()
        return report

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('root');parser.add_argument('--pause',action='store_true')
    args=parser.parse_args();run(args.root,args.pause)
