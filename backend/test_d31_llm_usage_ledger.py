"""Offline token/cost accounting regressions; no paid model or payment calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import uuid
import subprocess
import sys
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.infra.llm_pricing import catalog, price_usage, provider_host, crosses_band
from app.infra.llm_usage_ledger import (
    UsageLedger, UsageRequest, normalize_usage, usage_scope, accounted_request,
    accounted_call, get_ledger, billing_stage, AccountingError,
)
from app.infra.async_requests import bounded_request
from app.engine.cleaners.semantics_translator import SemanticsTranslator, TranslationStats


AT = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)  # Fri 10:00 Beijing, peak
HOST = 'https://api.deepseek.com/v1'
MODEL = 'deepseek-flash'


def response(id='provider-response', model=MODEL, content='{"results":[{"id":0,"translation":"译文"}]}', **usage):
    values = dict(prompt_tokens=100, completion_tokens=30, total_tokens=130,
                  prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20,
                  completion_tokens_details={'reasoning_tokens': 10})
    values.update(usage)
    return N(id=id, model=model, usage=values, choices=[N(message=N(content=content))])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = create_engine('sqlite:///'+str(self.root/'jobs.db'), connect_args={'check_same_thread':False})
        self.addCleanup(self.engine.dispose)
        self.ledger = get_ledger(self.engine)
        self.clock = patch('app.infra.llm_usage_ledger.now', return_value=AT)
        self.clock.start(); self.addCleanup(self.clock.stop)

    def call(self, resp=None, *, job='book', attempt='one', stage='body', base=HOST, model=MODEL):
        with usage_scope(job, attempt, engine=self.engine):
            return asyncio.run(accounted_request(AsyncMock(return_value=resp or response())(),
                                                model=model, base_url=base, stage=stage))

    def summary(self, job='book', **stats):
        return self.ledger.summary(job, stats or None)

    def test_peak_exact_decimal_cache_split_and_reasoning_not_double_charged(self):
        self.call()
        value=self.summary()
        self.assertEqual(value['calculated_totals'], {'CNY':'0.0002832'})
        self.assertEqual(value['prompt_tokens'],100); self.assertEqual(value['completion_tokens'],30)
        self.assertEqual(value['cache_hit_tokens'],80); self.assertEqual(value['cache_miss_tokens'],20)
        self.assertEqual(value['coverage'],'complete')

    def test_off_peak_half_price_weekend_and_legacy_flash_alias(self):
        with patch('app.infra.llm_usage_ledger.now',return_value=AT+timedelta(days=1)):
            self.call(response(model=MODEL), model='deepseek-v4-flash')
        self.assertEqual(self.summary()['calculated_totals'], {'CNY':'0.0001416'})

    def test_pro_uses_distinct_rate(self):
        self.call(response(model='deepseek-v4-pro'),model='deepseek-v4-pro')
        self.assertEqual(self.summary()['calculated_totals'], {'CNY':'0.001014'})

    def test_vendor_unknown_never_official_rate_or_zero(self):
        self.call(base='https://dashscope.aliyuncs.com/compatible-mode/v1')
        self.assertEqual(self.summary()['calculated_totals'],{})
        self.assertEqual(self.summary()['pending_reasons'],{'unknown_rate':1})
        self.assertEqual(self.summary()['coverage'],'incomplete')

    def test_unknown_model_never_zero(self):
        self.call(response(model='other'),model='other')
        self.assertEqual(self.summary()['pending_reasons'],{'unknown_rate':1})
        self.assertIsNone(TranslationStats(prompt_tokens=100).estimate_cost(MODEL))
        self.assertIn('待核实',TranslationStats().summary(MODEL))

    def test_missing_usage_retains_request(self):
        self.call(N(id='missing',model=MODEL,usage=None))
        self.assertEqual(self.summary()['pending_requests'],1)
        self.assertFalse(self.summary()['tokens_complete'])
        self.assertIsNone(self.ledger.requests('book')[0]['prompt_tokens'])

    def test_cache_unknown_not_assumed_miss(self):
        self.call(response(prompt_cache_hit_tokens=None,prompt_cache_miss_tokens=None))
        self.assertEqual(self.summary()['pending_reasons'],{'cache_split_missing':1})
        self.assertTrue(self.summary()['tokens_complete']); self.assertFalse(self.summary()['cache_tokens_complete'])

    def test_cache_one_side_and_details_derived(self):
        self.call(response(prompt_cache_hit_tokens=None,prompt_cache_miss_tokens=None,
                           prompt_tokens_details={'cached_tokens':80}))
        self.assertEqual(self.summary()['calculated_totals'], {'CNY':'0.0002832'})

    def test_invalid_usage_counters_and_contradictions_not_free(self):
        for usage in [dict(prompt_tokens=True),dict(prompt_tokens=-1),dict(prompt_tokens='100'),
                      dict(prompt_tokens=2**63),dict(total_tokens=999),dict(prompt_cache_hit_tokens=101),
                      dict(prompt_cache_miss_tokens=21),dict(prompt_tokens_details={'cached_tokens':70}),
                      dict(prompt_tokens_details={'cached_tokens':-1}),
                      dict(completion_tokens_details={'reasoning_tokens':31})]:
            with self.subTest(usage=usage):
                self.assertEqual(normalize_usage(response(**usage))['usage_status'],'invalid')

    def test_historical_prices_not_retroactively_applied(self):
        with patch('app.infra.llm_usage_ledger.now',return_value=AT-timedelta(days=1)):
            self.call()
        self.assertEqual(self.summary()['pending_reasons'],{'historical_rate_unknown':1})

    def test_crossing_band_even_same_endpoints_remains_pending(self):
        reservation=self.ledger.begin('book','one','body',HOST,MODEL)
        with patch('app.infra.llm_usage_ledger.now',return_value=AT+timedelta(hours=5)):
            self.ledger.finish(reservation,response())
        self.assertEqual(self.summary()['pending_reasons'],{'time_band_ambiguous':1})
        self.assertTrue(crosses_band(AT,AT+timedelta(hours=5)))

    def test_actual_model_mismatch_pending(self):
        self.call(response(model='deepseek-v4-pro'))
        self.assertEqual(self.summary()['pending_reasons'],{'model_mismatch':1})

    def test_attempts_and_stages_sum_without_resets(self):
        for i,stage in enumerate(('book_profile','glossary','book_title','body','rescue','semantic_review','style_guide','literary_polish','literary_verify','precision_polish')):
            self.call(response(id=str(i)),attempt='one' if i<5 else 'two',stage=stage)
        value=self.summary(attempt_id='two',translation_attempt=2)
        self.assertEqual(value['requests'],10);self.assertEqual(value['coverage'],'complete')
        self.assertEqual(len(value['stages']),10)
        self.assertEqual(Decimal(value['calculated_totals']['CNY']),Decimal('0.0002832')*10)

    def test_finished_twice_idempotent_and_persistent_new_instance(self):
        reservation=self.ledger.begin('book','one','body',HOST,MODEL)
        self.ledger.finish(reservation,response());self.ledger.finish(reservation,response())
        other=UsageLedger(self.engine)
        self.assertEqual(other.summary('book')['requests'],1)

    def test_prepaid_scope_and_all_local_cache_no_incremental_cost(self):
        with usage_scope('book','preflight',engine=self.engine):pass
        self.assertEqual(self.summary(attempt_id='not-yet-run',translation_attempt=1)['historical_untracked_attempts'],0)
        with usage_scope('book','one',engine=self.engine):pass
        self.assertEqual(self.summary(attempt_id='one',translation_attempt=1)['coverage'],'complete')
        self.assertEqual(self.summary()['requests'],0)

    def test_historical_attempts_and_resume_partial_coverage(self):
        self.call()
        value=self.summary(attempt_id='one',translation_attempt=2,cost_history=[{'attempt_id':'old','prompt_tokens':9}])
        self.assertEqual(value['historical_untracked_attempts'],1);self.assertEqual(value['coverage'],'incomplete')
        with usage_scope('oldbook','resumed',engine=self.engine,prior_usage_untracked=True):pass
        self.assertEqual(self.summary('oldbook')['coverage'],'incomplete')
        self.assertEqual(self.summary('never-recorded')['coverage'],'historical_unknown')

    def test_in_flight_process_crash_stays_pending(self):
        self.ledger.begin('book','one','body',HOST,MODEL)
        row=self.ledger.requests('book')[0]
        self.assertEqual(row['request_status'],'in_flight');self.assertIsNone(row['calculated_cost'])

    def test_timeout_cancellation_retains_unknown_charge(self):
        async def request():
            await asyncio.sleep(10)
        async def run():
            with usage_scope('book','one',engine=self.engine):
                with self.assertRaises(asyncio.TimeoutError):
                    await bounded_request(accounted_request(request(),model=MODEL,base_url=HOST),timeout=.01)
        asyncio.run(run())
        row=self.ledger.requests('book')[0]
        self.assertEqual(row['request_status'],'cancelled');self.assertIsNone(row['calculated_cost'])

    def test_provider_error_not_stored_as_zero_and_no_error_text(self):
        async def request():
            error=ValueError('secret-key book contents')
            error.status_code=400;error.request_id='provider-error-request'
            raise error
        with usage_scope('book','one',engine=self.engine):
            with self.assertRaises(ValueError):asyncio.run(accounted_request(request(),model=MODEL,base_url=HOST))
        text=json.dumps(self.ledger.requests('book'))
        self.assertNotIn('secret-key',text);self.assertIn('ValueError',text)
        self.assertIn('provider-error-request',text)
        self.assertEqual(self.ledger.requests('book')[0]['http_status'],400)
        self.assertEqual(self.summary()['pending_reasons'],{'usage_missing':1})

    def test_accounting_start_failure_no_paid_call(self):
        operation=AsyncMock(return_value=response())
        with usage_scope('book','one',engine=self.engine),patch.object(self.ledger,'begin',side_effect=RuntimeError('db')):
            with self.assertRaises(AccountingError):asyncio.run(accounted_request(operation(),model=MODEL,base_url=HOST))
        operation.assert_not_awaited()

    def test_accounting_finish_failure_retains_reservation_no_retry(self):
        operation=AsyncMock(return_value=response())
        with usage_scope('book','one',engine=self.engine),patch.object(self.ledger,'finish',side_effect=RuntimeError('db')):
            with self.assertRaises(AccountingError):asyncio.run(accounted_request(operation(),model=MODEL,base_url=HOST))
        self.assertEqual(operation.await_count,1);self.assertEqual(self.summary()['pending_reasons'],{'in_flight':1})

    def test_parallel_books_and_stages_context_isolated(self):
        @billing_stage('book_title')
        async def title(id):return await accounted_request(AsyncMock(return_value=response(id=id))(),model=MODEL,base_url=HOST)
        def work(id):
            with usage_scope(id,'one',engine=self.engine):asyncio.run(title(id))
        with ThreadPoolExecutor(max_workers=4) as executor:list(executor.map(work,['a','b','c','d']))
        for id in ['a','b','c','d']:
            self.assertEqual(self.summary(id)['requests'],1)
            self.assertEqual(self.ledger.requests(id)[0]['stage'],'book_title')

    def test_invalid_json_billable_usage_recorded_before_retry(self):
        with patch.dict(os.environ,{'OPENAI_BASE_URL':HOST,'OPENAI_MAX_RETRIES':'2','OPENAI_MODEL_FALLBACKS':'',
                                   'OPENAI_BASE_URL_FALLBACKS':'','OPENAI_API_KEY':'dummy','LLM_PROVIDERS_JSON':'[]'}):
            t=SemanticsTranslator(model=MODEL)
        create=AsyncMock(side_effect=[response(id='bad',content='garbage'),response(id='ok')])
        with usage_scope('book','one',engine=self.engine),patch.object(t,'_get_client',return_value=N(chat=N(completions=N(create=create)))),patch('asyncio.sleep',new=AsyncMock()):
            value,meta=asyncio.run(t._call_llm_json_batch([{'id':0,'html':'Source text.'}]))
        self.assertEqual(value[0],'译文');self.assertEqual(self.summary()['requests'],2)
        self.assertEqual(t.stats.prompt_tokens,200);self.assertEqual(t.stats.completion_tokens,60)

    def test_sync_precision_polish_envelope_recorded(self):
        with usage_scope('book','one',engine=self.engine):
            result=accounted_call(lambda: response(),model=MODEL,base_url=HOST,stage='precision_polish')
        self.assertEqual(result.id,'provider-response');self.assertEqual(self.summary()['requests'],1)

    def test_no_scope_does_not_write_but_stats_observer_still_runs(self):
        observer=[]
        asyncio.run(accounted_request(AsyncMock(return_value=response())(),model=MODEL,base_url=HOST,usage_observer=observer.append))
        self.assertEqual(self.summary()['requests'],0);self.assertEqual(observer[0]['prompt_tokens'],100)

    def test_url_credentials_book_text_and_raw_response_never_persist(self):
        self.call(response(content='private book'),base='https://username:secret@api.deepseek.com/v1?key=secret')
        text=json.dumps(self.ledger.requests('book'))
        self.assertNotIn('secret',text);self.assertNotIn('private book',text);self.assertNotIn('username',text)
        self.assertEqual(provider_host('https://u:p@api.deepseek.com/v1?key=x'),'api.deepseek.com')

    def test_provider_response_replay_not_silently_double_priced(self):
        self.call();self.call()
        self.assertEqual(self.summary()['priced_requests'],1)
        self.assertEqual(self.summary()['pending_reasons'],{'duplicate_response_id':1})

    def test_bill_import_match_dry_run_idempotency_and_no_guess(self):
        self.call();row=self.ledger.requests('book')[0]
        kwargs=dict(response_id=row['response_id'],provider=row['provider'],amount='0.0002',currency='CNY',source_sha256='a'*64)
        self.ledger.import_bill(row['id'],dry_run=True,**kwargs)
        self.assertEqual(self.summary()['bill_imported_totals'],{})
        self.ledger.import_bill(row['id'],**kwargs);self.ledger.import_bill(row['id'],**kwargs)
        self.assertEqual(self.summary()['bill_imported_totals'],{'CNY':'0.0002'})
        for changes in [dict(amount='0.1'),dict(response_id='wrong'),dict(provider='wrong'),dict(currency='XXX!'),dict(amount='NaN')]:
            with self.assertRaises(ValueError):self.ledger.import_bill(row['id'],**{**kwargs,**changes})

    def test_configured_other_provider_currency_separate_and_snapshot_retained(self):
        tariffs=self.root/'prices.json'
        tariff=dict(provider='other.example',model=MODEL,currency='USD',version='test-v1',valid_from='2026-09-18T00:00:00+08:00',cache_hit='1',cache_miss='2',output='3',source='https://other.example/pricing',checked_at='2026-09-18')
        tariffs.write_text(json.dumps([tariff]))
        with patch.dict(os.environ,{'LLM_PRICING_FILE':str(tariffs)}):
            self.call(response(id='other'),base='https://other.example/v1')
        self.call(response(id='official'))
        self.assertEqual(self.summary()['calculated_totals'],{'USD':'0.00021','CNY':'0.0002832'})
        with Session(self.engine) as s:
            row=s.scalar(select(UsageRequest).where(UsageRequest.response_id=='other'))
            self.assertEqual(json.loads(row.rate_snapshot)['version'],'test-v1')

    def test_pagination_stable_and_request_ids_per_call(self):
        for i in range(5):self.call(response(id=str(i)))
        ids=[r['id'] for p in (1,2,3) for r in self.ledger.requests('book',p,2)]
        self.assertEqual(len(ids),5);self.assertEqual(len(set(ids)),5)

    def test_flat_input_tariff_does_not_require_unpriced_cache_split(self):
        tariff=dict(provider='api.deepseek.com',model=MODEL,currency='CNY',version='flat',valid_from='2026-09-18T00:00:00+08:00',cache_hit='1',cache_miss='1',output='4',source='https://example.test/pricing',checked_at='2026-09-18')
        value=price_usage('api.deepseek.com',MODEL,MODEL,normalize_usage(response(prompt_cache_hit_tokens=None,prompt_cache_miss_tokens=None)),AT,AT,[tariff])
        self.assertEqual(value['calculated_cost'],'0.00022');self.assertEqual(value['price_status'],'priced')

    def test_price_version_change_during_request_not_guessed(self):
        prices=catalog()
        prices.append({**prices[0],'valid_from':(AT+timedelta(minutes=1)).isoformat(),'version':'new'})
        value=price_usage('api.deepseek.com',MODEL,MODEL,normalize_usage(response()),AT,AT+timedelta(minutes=2),prices)
        self.assertEqual(value['price_status'],'rate_boundary_ambiguous')

    def test_profile_json_compat_retry_both_recorded(self):
        from app.domain.book_profile_service import profile_book_async
        create=AsyncMock(side_effect=[ValueError('response_format unsupported'),response(content='{"genre":"fiction","confidence":0.9,"recommended_strategy":"neutral_faithful"}')])
        client=N(chat=N(completions=N(create=create)),close=AsyncMock())
        payload={'metadata':{},'toc':[],'samples':[{'text':'A bounded source sample.'}]}
        with usage_scope('book','preflight',engine=self.engine),patch.dict(os.environ,{'OPENAI_API_KEY':'dummy-test','OPENAI_BASE_URL':HOST,'EPUB_BOOK_PROFILER_ENABLED':'1','EPUB_BOOK_PROFILER_MODEL':MODEL,'OPENAI_DISABLE_JSON_RESPONSE_FORMAT':'0'}),patch('app.domain.book_profile_service.AsyncOpenAI',return_value=client),patch('app.domain.book_profile_service.build_profiler_input',return_value=(payload,{})):
            asyncio.run(profile_book_async(epub_path='unused.epub',manifest={},model=MODEL))
        self.assertEqual(self.summary()['requests'],2)
        self.assertEqual(self.summary()['stages']['book_profile']['requests'],2)
        self.assertEqual(self.summary()['priced_requests'],1)

    def test_glossary_entry_actual_usage_not_metrics_estimate(self):
        from app.engine.glossary_extractor import translate_glossary,GlossaryCandidate
        create=AsyncMock(return_value=response(content='{"translations":{"Smith":"史密斯"}}'))
        client=N(chat=N(completions=N(create=create)),close=AsyncMock())
        with usage_scope('book','preflight',engine=self.engine),patch.dict(os.environ,{'OPENAI_API_KEY':'dummy-test','OPENAI_BASE_URL':HOST,'OPENAI_MODEL':MODEL}),patch('openai.AsyncOpenAI',return_value=client):
            value=asyncio.run(translate_glossary([GlossaryCandidate('Smith',2)]))
        self.assertEqual(value,{'Smith':'史密斯'})
        self.assertEqual(self.summary()['stages']['glossary']['requests'],1)
        self.assertEqual(self.summary()['prompt_tokens'],100)

    def test_preflight_scope_uses_passed_order_database_in_worker_thread(self):
        from app.domain.translation_preflight_service import build_translation_preflight
        def analyze(**kwargs):
            asyncio.run(accounted_request(AsyncMock(return_value=response())(),model=MODEL,base_url=HOST))
            return {'status':'ready'}
        with patch('app.domain.translation_preflight_service._build_translation_preflight',side_effect=analyze):
            with ThreadPoolExecutor(max_workers=1) as pool:
                result=pool.submit(build_translation_preflight,epub_path='unused',job_id='book',target_lang='zh-CN',translation_model=MODEL,requested_strategy='auto',billing_engine=self.engine).result()
        self.assertEqual(result['status'],'ready')
        self.assertEqual(self.ledger.requests('book')[0]['attempt_id'],'preflight')

    def test_bill_duplicate_response_cannot_be_assigned_twice(self):
        self.call();self.call()
        rows=self.ledger.requests('book')
        kwargs=dict(response_id=rows[0]['response_id'],provider=rows[0]['provider'],currency='CNY',amount='0.1',source_sha256='a'*64)
        self.ledger.import_bill(rows[0]['id'],**kwargs)
        with self.assertRaises(ValueError):self.ledger.import_bill(rows[1]['id'],**kwargs)

    def test_official_alias_mapping_not_assumed_on_another_provider(self):
        rows=[dict(provider='other.example',model='deepseek-v4-flash',currency='CNY',version='external',valid_from='2026-09-18T00:00:00+08:00',cache_hit='1',cache_miss='1',output='4',source='https://other.example/pricing',checked_at='2026-09-18')]
        value=price_usage('other.example','deepseek-v4-flash','deepseek-v4-flash',normalize_usage(response()),AT,AT,rows)
        self.assertEqual(value['price_status'],'priced');self.assertEqual(value['calculated_cost'],'0.00022')

    def test_outer_job_marks_accounting_abort_failed_and_never_retries_model(self):
        from contextlib import nullcontext
        from app.job_runner import run_job
        from app.models import Job,JobStatus,OutputMode,ErrorCode
        from app.storage import JobStore
        store=JobStore();store._engine=self.engine
        source=self.root/'book.epub';source.write_bytes(b'offline-fixture')
        job=Job(id=uuid.uuid4().hex,source_filename='book.epub',trace_id='offline',input_path=str(source),
                output_mode=OutputMode.simplified,enable_translation=True,status=JobStatus.pending,
                translation_stats={'attempt_id':'one','translation_attempt':1})
        store.add(job)
        operation=AsyncMock(return_value=response())
        def translate(**kwargs):
            return asyncio.run(accounted_request(operation(),model=MODEL,base_url=HOST))
        with patch('app.job_runner.job_store',store),patch('app.job_runner.OUTPUT_DIR',self.root),patch('app.job_runner.execution_lease',return_value=nullcontext(N(assert_owned=lambda:None,owner='offline'))),patch('app.domain.fast_translation_runner.run_fast_translation_job',side_effect=translate),patch.object(self.ledger,'finish',side_effect=RuntimeError('db')),patch('app.job_runner.report_error'),patch('app.job_runner.notify_job_completed'),patch('app.job_runner.logger.exception'):
            run_job(job.id,'one')
        self.assertEqual(job.status,JobStatus.failed);self.assertEqual(job.error_code,ErrorCode.TRANSLATION_FAILED)
        self.assertEqual(operation.await_count,1)
        self.assertEqual(self.summary(job.id)['pending_reasons'],{'in_flight':1})

    def test_response_recorded_even_if_cancellation_checked_after_return(self):
        from app.cancellation import JobCancelled
        cancelled=False
        async def request():
            nonlocal cancelled
            cancelled=True
            return response()
        async def run():
            with usage_scope('book','one',engine=self.engine):
                with self.assertRaises(JobCancelled):
                    await bounded_request(accounted_request(request(),model=MODEL,base_url=HOST),timeout=1,cancel_check=lambda:cancelled)
        asyncio.run(run())
        self.assertEqual(self.summary()['priced_requests'],1)
        self.assertEqual(self.ledger.requests('book')[0]['request_status'],'response')

    def test_bill_import_cli_dry_run_then_apply_no_api_calls(self):
        self.call();row=self.ledger.requests('book')[0]
        matches=self.root/'matches.json';source=self.root/'bill.json'
        source.write_text('{"fixture":"provider bill already manually checked"}')
        matches.write_text(json.dumps([dict(ledger_id=row['id'],response_id=row['response_id'],provider=row['provider'],currency='CNY',amount='0.0002')]))
        script=Path(__file__).parent/'scripts/import_llm_bill.py'
        env={**os.environ,'DATABASE_URL':str(self.engine.url)}
        args=[sys.executable,str(script),str(matches),'--source-bill',str(source)]
        for apply in (False,True):
            process=subprocess.run(args+(['--apply'] if apply else []),env=env,text=True,capture_output=True,timeout=10)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertEqual(json.loads(process.stdout)['applied'],apply)
            self.assertEqual(self.summary()['bill_matched_requests'],1 if apply else 0)

    def test_old_preparations_not_hidden_by_zero_body_counters(self):
        old={'attempt_id':'one','api_calls':0,'prompt_tokens':0,'book_profile':{'usage':{'prompt_tokens':100}}}
        with usage_scope('book','one',engine=self.engine,existing_stats=old):pass
        self.assertEqual(self.summary()['historical_untracked_attempts'],1)
        self.assertEqual(self.summary()['coverage'],'incomplete')
        with usage_scope('counter-only','one',engine=self.engine,existing_stats={'prompt_tokens':1,'api_calls':0}):pass
        self.assertEqual(self.summary('counter-only')['historical_untracked_attempts'],1)

    def test_new_preflight_not_double_classified_as_historical_gap(self):
        with usage_scope('book','preflight',engine=self.engine):pass
        with usage_scope('book','one',engine=self.engine,existing_stats={'translation_preflight':{'profile':{'usage':{'prompt_tokens':100}}}}):pass
        self.assertEqual(self.summary(attempt_id='one',translation_attempt=1)['historical_untracked_attempts'],0)
        self.assertEqual(self.summary()['coverage'],'complete')


if __name__=='__main__':unittest.main()
