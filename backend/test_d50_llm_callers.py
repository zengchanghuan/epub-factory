"""R12: real caller → gateway → ledger, with provider transport controlled offline."""
import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, patch

import httpx
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import create_engine

with patch('dotenv.load_dotenv', return_value=False):
    from app.cancellation import JobCancelled
    from app.domain import book_profile_service as profile
    from app.engine import glossary_extractor as glossary
    from app.engine.cleaners import llm_polish as polish
    from app.engine.cleaners.semantics_translator import SemanticsTranslator, _within_request_budget
    from app.infra.llm_gateway import GatewayControlError, dispatch_budget_scope
    from app.infra.llm_token_bucket import DistributedLLMTokenBucket, TokenBucketLease
    from app.infra.llm_route_health import DistributedRouteHealth
    from app.infra.llm_usage_ledger import AccountingError, get_ledger, usage_scope


MODEL = 'deepseek-flash'
HOST = 'https://api.deepseek.com/v1'
USAGE = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
         'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 100}


def response(content):
    return N(id='offline-response', model=MODEL, usage=N(**USAGE),
             choices=[N(message=N(content=content), finish_reason='stop')])


def keep_response(payload):
    data = json.loads(payload['messages'][-1]['content'])
    decisions = [{'id': item['id'], 'source': item['source'], 'action': 'keep',
                  'replacement': item['source']} for item in data['occurrences']]
    return {'id': 'offline-polish', 'model': MODEL, 'usage': dict(USAGE),
            'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'decisions': decisions}, ensure_ascii=False)}}]}


class LLMCallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='llm-callers-')
        self.addCleanup(temporary.cleanup)
        self.engine = create_engine('sqlite:///' + str(Path(temporary.name) / 'ledger.db'))
        self.addCleanup(self.engine.dispose)
        self.ledger = get_ledger(self.engine)
        self._patch(patch.dict(os.environ, {
            'OPENAI_API_KEY': 'offline-only', 'OPENAI_BASE_URL': HOST, 'OPENAI_MODEL': MODEL,
            'OPENAI_MODEL_FALLBACKS': '', 'OPENAI_BASE_URL_FALLBACKS': '',
            'OPENAI_MAX_RETRIES': '2', 'OPENAI_TIMEOUT_EXTRA_RETRIES': '0',
            'OPENAI_DISABLE_JSON_RESPONSE_FORMAT': '0',
            'EPUB_DEFAULT_TRANSLATION_MODEL': MODEL,
            'EPUB_LLM_RATE_LIMITER_ENABLED': '0', 'EPUB_LLM_GLOBAL_HEALTH_ENABLED': '0',
            'EPUB_BOOK_PROFILER_ENABLED': '1', 'EPUB_BOOK_PROFILER_MODEL': MODEL,
            'EPUB_BOOK_PROFILER_TIMEOUT': '1', 'EPUB_GLOSSARY_REQUEST_TIMEOUT': '1',
            'EPUB_GLOSSARY_CONCURRENCY': '1', 'EPUB_PREFLIGHT_MAX_OUTPUT_TOKENS': '4096',
            'DEEPSEEK_API_KEY': 'offline-only', 'DEEPSEEK_BASE_URL': HOST, 'DEEPSEEK_MODEL': MODEL,
            'L4_API_TIMEOUT_SEC': '1', 'L4_MAX_INPUT_BYTES_PER_PARA': '8192',
            'L4_MAX_TOKENS_PER_PARA': '4096', 'L4_MAX_TOKENS_PER_BOOK': '2000000',
        }, clear=True))
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            self._patch(patch(target, side_effect=AssertionError('External network forbidden')))
        self._patch(patch('app.engine.cleaners.semantics_translator.TranslationCache', return_value=Mock()))
        SemanticsTranslator._ROUTE_HEALTH.clear()
        self.acquisitions = []
        self.reconciliations = []

        def acquire_sync(instance, **kwargs):
            self.acquisitions.append(kwargs)
            return TokenBucketLease(estimated_tokens=kwargs['estimated_tokens'], enabled=True,
                                    token_key='offline-shared-bucket')

        async def acquire(instance, **kwargs):
            return acquire_sync(instance, **kwargs)

        def reconcile_sync(instance, lease, *, actual_tokens):
            self.reconciliations.append(actual_tokens)

        async def reconcile(instance, lease, *, actual_tokens):
            reconcile_sync(instance, lease, actual_tokens=actual_tokens)

        self._patch(patch.object(DistributedLLMTokenBucket, 'acquire', new=acquire))
        self._patch(patch.object(DistributedLLMTokenBucket, 'acquire_sync', new=acquire_sync))
        self._patch(patch.object(DistributedLLMTokenBucket, 'reconcile', new=reconcile))
        self._patch(patch.object(DistributedLLMTokenBucket, 'reconcile_sync', new=reconcile_sync))
        self.health_success = self._patch(patch.object(DistributedRouteHealth, 'record_success', return_value=True))
        self.health_failure = self._patch(patch.object(DistributedRouteHealth, 'record_failure', return_value=True))

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def scope(self):
        return usage_scope('caller-job', 'caller-attempt', engine=self.engine)

    def rows(self):
        return self.ledger.requests('caller-job')

    def translator(self, create):
        translator = SemanticsTranslator(model=MODEL)
        translator._get_client = Mock(return_value=N(chat=N(completions=N(create=create))))
        return translator

    def call_translation(self, create, *, cancel_check=None):
        translator = self.translator(create)
        translator.cancel_check = cancel_check
        return asyncio.run(translator._call_llm_json_batch([{'id': 0, 'html': 'Controlled source.'}]))

    def call_profile(self, create, *, cancel_check=None):
        client = N(chat=N(completions=N(create=create)), close=AsyncMock())
        payload = {'metadata': {}, 'toc': [], 'samples': [{'text': 'Controlled source.'}]}
        with patch.object(profile, 'AsyncOpenAI', return_value=client), \
             patch.object(profile, 'build_profiler_input', return_value=(payload, {})):
            return profile.profile_book(epub_path='unused.epub', manifest={}, model=MODEL,
                                        cancel_check=cancel_check)

    def call_glossary(self, create, *, cancel_check=None):
        client = N(chat=N(completions=N(create=create)), close=AsyncMock())
        with patch('openai.AsyncOpenAI', return_value=client):
            return asyncio.run(glossary.translate_glossary(
                [glossary.GlossaryCandidate('Smith', 2)], cancel_check=cancel_check))

    def call_polish(self, create, *, cancel_check=None):
        plan = polish.plan_document(b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
                                    + '服务生'.encode() + b'</p></body></html>')
        with polish.LLMPolisher(cancel_check=cancel_check) as polisher:
            polisher._request = create
            return polisher.polish_document(plan)

    def creators(self):
        return (
            (self.call_translation, AsyncMock(return_value=response('{"results":[{"id":0,"translation":"译文"}]}'))),
            (self.call_profile, AsyncMock(return_value=response('{"genre":"fiction","confidence":0.9,"recommended_strategy":"neutral_faithful"}'))),
            (self.call_glossary, AsyncMock(return_value=response('{"translations":{"Smith":"史密斯"}}'))),
            (self.call_polish, Mock(side_effect=keep_response)),
        )

    def test_all_four_callers_use_same_quota_and_real_stage_ledger(self):
        with self.scope():
            for caller, create in self.creators():
                caller(create)
        self.assertEqual(len(self.acquisitions), 4)
        self.assertEqual(len({(x['provider'], x['model']) for x in self.acquisitions}), 1)
        self.assertEqual(self.reconciliations, [120] * 4)
        self.assertEqual({row['stage'] for row in self.rows()},
                         {'body', 'book_profile', 'glossary', 'precision_polish'})
        self.assertEqual(self.health_success.call_count, 4)
        self.assertEqual(self.health_failure.call_count, 0)

    def test_translation_json_compat_each_physical_request_has_quota_budget_and_ledger(self):
        create = AsyncMock(side_effect=[ValueError('response_format unsupported'),
                                       response('{"results":[{"id":0,"translation":"译文"}]}')])
        translator = self.translator(create)
        observed = []
        with self.scope():
            result = asyncio.run(_within_request_budget(2, lambda: translator._call_llm_json_batch(
                [{'id': 0, 'html': 'Controlled source.'}]), on_request=observed.append))
        self.assertEqual(result[0], {0: '译文'})
        self.assertEqual(create.await_count, 2)
        self.assertEqual(observed, [1, 2])
        self.assertEqual(translator.stats.api_calls, 2)
        self.assertEqual(translator.stats.global_rate_limit_acquisitions, 2)
        self.assertEqual(len(self.acquisitions), 2)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.reconciliations[-1], 120)
        self.assertEqual(self.health_success.call_count, 1)
        self.assertEqual(self.health_failure.call_count, 1)

    def test_translation_invalid_json_keeps_both_usage_and_only_transport_health(self):
        create = AsyncMock(side_effect=[response('not json'),
                                       response('{"results":[{"id":0,"translation":"译文"}]}')])
        with self.scope(), patch('asyncio.sleep', new=AsyncMock()):
            self.call_translation(create)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(len(self.acquisitions), 2)
        self.assertEqual(self.reconciliations, [120, 120])
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.health_success.call_count, 2)
        self.assertEqual(self.health_failure.call_count, 0)

    def test_profile_compat_resend_has_two_physical_reservations_and_output_cap(self):
        create = AsyncMock(side_effect=[ValueError('response_format unsupported'),
                                       response('{"genre":"fiction","confidence":0.9,"recommended_strategy":"neutral_faithful"}')])
        with self.scope():
            self.call_profile(create)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(len(self.acquisitions), 2)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(x.kwargs['max_tokens'] == 4096 for x in create.await_args_list))
        self.assertIn('response_format', create.await_args_list[0].kwargs)
        self.assertNotIn('response_format', create.await_args_list[1].kwargs)

    def test_glossary_output_cap_is_explicit(self):
        create = AsyncMock(return_value=response('{"translations":{"Smith":"史密斯"}}'))
        with self.scope(), patch.dict(os.environ, {'EPUB_PREFLIGHT_MAX_OUTPUT_TOKENS': '1234'}):
            self.assertEqual(self.call_glossary(create), {'Smith': '史密斯'})
        self.assertEqual(create.await_args.kwargs['max_tokens'], 1234)

    def test_invalid_preflight_output_cap_is_zero_request_not_silent_fallback(self):
        for bad in ('0', '-1', '16385', '4.5', 'invalid'):
            for caller in (self.call_profile, self.call_glossary):
                with self.subTest(value=bad, caller=caller.__name__), self.scope(), \
                     patch.dict(os.environ, {'EPUB_PREFLIGHT_MAX_OUTPUT_TOKENS': bad}):
                    create = AsyncMock()
                    with self.assertRaises(GatewayControlError):
                        caller(create)
                    create.assert_not_called()
        self.assertEqual(self.acquisitions, [])
        self.assertEqual(self.rows(), [])

    def test_cancel_before_each_caller_is_zero_transport(self):
        for caller, create in self.creators():
            with self.subTest(caller=caller.__name__), self.scope():
                with self.assertRaises(JobCancelled):
                    caller(create, cancel_check=lambda: True)
                create.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_soft_time_limit_is_never_profile_or_glossary_fallback(self):
        for caller, create in self.creators():
            create.side_effect = SoftTimeLimitExceeded()
            with self.subTest(caller=caller.__name__), self.scope():
                with self.assertRaises(SoftTimeLimitExceeded):
                    caller(create)
                self.assertEqual(create.call_count, 1)

    def test_accounting_reserve_failure_is_zero_transport_across_callers(self):
        for caller, create in self.creators():
            with self.subTest(caller=caller.__name__), self.scope(), \
                 patch('app.infra.llm_usage_ledger._reserve', side_effect=AccountingError('offline reserve failure')):
                with self.assertRaises(AccountingError):
                    caller(create)
                create.assert_not_called()

    def test_accounting_finish_failure_never_retries_or_falls_back(self):
        for caller, create in self.creators():
            with self.subTest(caller=caller.__name__), self.scope(), \
                 patch('app.infra.llm_usage_ledger._finish', side_effect=AccountingError('offline finish failure')):
                with self.assertRaises(AccountingError):
                    caller(create)
                self.assertEqual(create.call_count, 1)

    def test_gateway_budget_refusal_is_not_business_fallback(self):
        def reject(*args, **metadata):
            raise GatewayControlError('offline budget refusal')
        for caller, create in self.creators():
            with self.subTest(caller=caller.__name__), self.scope(), dispatch_budget_scope(reject):
                with self.assertRaises(GatewayControlError):
                    caller(create)
                create.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_profile_absolute_deadline_interrupts_inflight(self):
        async def paused(**kwargs):
            await asyncio.sleep(60)
        create = AsyncMock(side_effect=paused)
        with self.scope(), patch.dict(os.environ, {'EPUB_BOOK_PROFILER_TIMEOUT': '0.01'}):
            result = self.call_profile(create)
        self.assertEqual(result['status'], 'fallback')
        self.assertEqual(create.await_count, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_profile_inflight_cancel_is_not_returned_as_fallback(self):
        stopped = False
        async def request(**kwargs):
            nonlocal stopped
            stopped = True
            return response('{"genre":"fiction","confidence":0.9,"recommended_strategy":"neutral_faithful"}')
        create = AsyncMock(side_effect=request)
        with self.scope():
            with self.assertRaises(JobCancelled):
                self.call_profile(create, cancel_check=lambda: stopped)
        self.assertEqual(create.await_count, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_preflight_budget_receives_real_cap_for_both_stages(self):
        reservations = []
        def reserve(estimated, **metadata):
            reservations.append((estimated, metadata))
        with self.scope(), dispatch_budget_scope(reserve):
            for caller, create in self.creators()[1:3]:
                caller(create)
        self.assertEqual(len(reservations), 2)
        for estimated, metadata in reservations:
            self.assertEqual(metadata['max_output_tokens'], 4096)
            self.assertEqual(metadata['model'], MODEL)
            self.assertEqual(metadata['base_url'], HOST)
            self.assertGreater(estimated, 4096)

    def test_sync_polish_cancel_after_response_retains_real_usage(self):
        stopped = False
        def request(payload):
            nonlocal stopped
            stopped = True
            return keep_response(payload)
        with self.scope():
            with self.assertRaises(JobCancelled):
                self.call_polish(Mock(side_effect=request), cancel_check=lambda: stopped)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['usage_status'], 'complete')
        self.assertEqual(self.reconciliations, [120])

    def test_sync_precision_retry_obeys_same_physical_quota(self):
        attempts = []
        def request(payload):
            attempts.append(payload)
            if len(attempts) == 1:
                exc = httpx.HTTPStatusError('offline unavailable',
                    request=httpx.Request('POST', HOST), response=httpx.Response(503))
                exc.status_code = 503
                raise exc
            return keep_response(payload)
        with self.scope(), patch.object(polish.time, 'sleep'):
            self.call_polish(Mock(side_effect=request))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.acquisitions), 2)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.reconciliations[-1], 120)


if __name__ == '__main__':
    unittest.main(verbosity=2)
