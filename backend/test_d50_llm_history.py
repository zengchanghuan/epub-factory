"""R12 SHA-pinned book samples through actual callers/gateway/accounting.

Provider replies and Redis transport are controlled locally. This validates
physical-call governance, not new literary translation quality or new EPUB
delivery. D41/D42 separately exercise complete real-book artifact/QA paths.
Historical originals and previously delivered outputs are never overwritten.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, patch

from test_d37_entitlement_history import BOOKS, sha256


class SharedRedisTransport:
    """Atomic in-memory Redis reply fixture, not a live Redis/Lua assertion."""
    def __init__(self):
        self.lock = threading.Lock()
        self.acquired = []
        self.adjusted = []
        self.leases = {}
        self.used_requests = {}

    def eval(self, script, count, *args):
        with self.lock:
            if count == 3:
                request_key, token_key, lease_key, rpm, tpm, estimate, ttl, identity = args
                used = self.used_requests.get(request_key, 0)
                if used >= rpm:
                    return [0, 1000]
                self.used_requests[request_key] = used + 1
                self.leases[lease_key] = (token_key, estimate)
                self.acquired.append((request_key, token_key, lease_key, estimate))
                return [1, 0]
            if count == 2:
                token_key, lease_key, capacity, actual = args
                recorded = self.leases.pop(lease_key, None)
                if recorded is None:
                    return 0
                if recorded[0] != token_key:
                    raise AssertionError('Wrong token bucket reconciliation')
                self.adjusted.append((token_key, actual))
                return 1
            raise AssertionError('Unexpected Redis transport operation')


@unittest.skipUnless(os.environ.get('EPUB_HISTORY_UPLOAD_DIR') and os.environ.get('EPUB_HISTORY_OUTPUT_DIR'),
                     'Explicit SHA-pinned historical upload/output directories are required')
class LLMHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix='epub-r12-history-')))
        cls.sources, cls.artifacts = {}, {}
        for book in BOOKS:
            for kind, env, target in (('input', 'EPUB_HISTORY_UPLOAD_DIR', cls.sources),
                                       ('output', 'EPUB_HISTORY_OUTPUT_DIR', cls.artifacts)):
                path = Path(os.environ[env]).resolve() / book[kind]
                if not path.is_file() or sha256(path) != book[kind + '_sha256']:
                    raise AssertionError('Historical fixture missing or changed: ' + book['key'] + ' ' + kind)
                target[book['key']] = path
        cls.stack.enter_context(patch('dotenv.load_dotenv', return_value=False))
        cls.stack.enter_context(patch.dict(os.environ, {
            'DATABASE_URL': 'sqlite:///' + str(cls.root / 'bootstrap.sqlite3'),
            'OPENAI_API_KEY': 'offline-history-only', 'OPENAI_BASE_URL': 'https://api.deepseek.com/v1',
            'OPENAI_MODEL': 'deepseek-flash', 'EPUB_DEFAULT_TRANSLATION_MODEL': 'deepseek-flash',
            'OPENAI_BASE_URL_FALLBACKS': '', 'OPENAI_MODEL_FALLBACKS': '', 'OPENAI_MAX_RETRIES': '1',
            'EPUB_BOOK_PROFILER_MODEL': 'deepseek-flash', 'EPUB_BOOK_PROFILER_ENABLED': '1',
            'EPUB_PREFLIGHT_MAX_OUTPUT_TOKENS': '4096', 'EPUB_GLOSSARY_CONCURRENCY': '1',
            'EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE': '1',
            'DEEPSEEK_API_KEY': 'offline-history-only', 'DEEPSEEK_BASE_URL': 'https://api.deepseek.com/v1',
            'DEEPSEEK_MODEL': 'deepseek-flash', 'L4_MAX_TOKENS_PER_BOOK': '2000000',
            'L4_MAX_INPUT_BYTES_PER_PARA': '8192', 'L4_MAX_TOKENS_PER_PARA': '4096',
            'EPUB_LLM_RATE_LIMITER_ENABLED': '1', 'EPUB_LLM_RATE_LIMIT_FAIL_OPEN': '0',
            'EPUB_LLM_RPM': '240', 'EPUB_LLM_TPM': '600000',
            'EPUB_LLM_GLOBAL_HEALTH_ENABLED': '0', 'REDIS_URL': 'redis://offline.invalid/0',
            'CELERY_BROKER_URL': '',
        }, clear=True))
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            cls.stack.enter_context(patch(target, side_effect=AssertionError('Historical gate forbids external network')))
        from sqlalchemy import create_engine
        from app.domain.manifest_service import build_manifest
        from app.engine.unpacker import EpubUnpacker
        from app.engine.cleaners.llm_polish import plan_document
        from app.engine.glossary_extractor import extract_candidates
        from app.engine.cleaners.semantics_translator import SemanticsTranslator
        from test_d50_llm_callers import response, keep_response

        cls.response, cls.keep_response = staticmethod(response), staticmethod(keep_response)
        cls.engine = create_engine('sqlite:///' + str(cls.root / 'usage.sqlite3'))
        cls.addClassCleanup(cls.engine.dispose)
        cls.stack.enter_context(patch('app.engine.cleaners.semantics_translator.TranslationCache', return_value=Mock()))
        cls.manifests, cls.samples, cls.plans, cls.candidates = {}, {}, {}, {}
        for key, path in cls.sources.items():
            manifest = build_manifest(str(path), 'history-' + key)
            if manifest.get('error'):
                raise AssertionError('Actual historical manifest failed: ' + key)
            chunks = [chunk for chapter in manifest['chapters'] if chapter.get('chapter_kind') == 'body'
                      for chunk in chapter.get('chunks') or []]
            cls.manifests[key] = manifest
            cls.samples[key] = next(chunk for chunk in chunks if len(chunk.get('text', '')) >= 80)
            candidates, stats = extract_candidates([chunk['text'] for chunk in chunks], min_count=1, max_terms=6)
            if not candidates:
                raise AssertionError('Historical sample contains no real glossary candidates: ' + key)
            cls.candidates[key] = candidates[:3]
            book = EpubUnpacker(str(path)).load_book()
            plans = [plan_document(item.get_content()) for item in book.get_items()
                     if item.get_type() == 9]
            cls.plans[key] = next((plan for plan in plans if plan.paragraphs), None)
        cls.rescue_sample = next(chunk for chapter in cls.manifests['double-helix']['chapters']
                                 for chunk in chapter.get('chunks') or []
                                 if 80 < len(chunk.get('text', '')) < 600
                                 and re.search(r'[A-Za-z]{4}', chunk.get('text', ''))
                                 and not re.search(r'\d', chunk.get('text', '')))
        cls.addClassCleanup(cls.assert_history_unchanged)

    @classmethod
    def assert_history_unchanged(cls):
        for book in BOOKS:
            for paths, kind in ((cls.sources, 'input'), (cls.artifacts, 'output')):
                if sha256(paths[book['key']]) != book[kind + '_sha256']:
                    raise AssertionError('Read-only historical file changed: ' + book['key'] + ' ' + kind)

    def setUp(self):
        from app.infra.llm_token_bucket import DistributedLLMTokenBucket
        self.redis = SharedRedisTransport()
        patcher = patch.object(DistributedLLMTokenBucket, '_client', return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.job_id = uuid.uuid4().hex
        self.actual_requests = 0
        self.source_hashes = []

    def scope(self):
        from app.infra.llm_usage_ledger import usage_scope
        return usage_scope(self.job_id, 'history-attempt', engine=self.engine)

    def translation_client(self):
        async def create(**kwargs):
            self.actual_requests += 1
            payload = json.loads(kwargs['messages'][1]['content'])
            self.source_hashes.append(hashlib.sha256(kwargs['messages'][1]['content'].encode()).hexdigest())
            results = [{'id': item['id'], 'translation':
                        ('这是用于检查补译调用边界的离线受控回应。' if item.get('text_node_rescue') else item['html'])}
                       for item in payload]
            return self.response(json.dumps({'results': results}, ensure_ascii=False))
        return N(chat=N(completions=N(create=create)))

    def exercise_book(self, key):
        from app.domain.book_profile_service import profile_book
        from app.engine.glossary_extractor import translate_glossary
        from app.engine.cleaners.semantics_translator import SemanticsTranslator
        from app.engine.cleaners.llm_polish import LLMPolisher
        async def profiler(**kwargs):
            self.actual_requests += 1
            payload = json.loads(kwargs['messages'][1]['content'])
            self.assertTrue(payload['samples'])
            return self.response('{"genre":"nonfiction","confidence":0.8,"recommended_strategy":"neutral_faithful"}')
        profile_client = N(chat=N(completions=N(create=profiler)), close=AsyncMock())
        with patch('app.domain.book_profile_service.AsyncOpenAI', return_value=profile_client):
            self.assertEqual(profile_book(epub_path=self.sources[key], manifest=self.manifests[key],
                                          model='deepseek-flash')['status'], 'ok')
        async def terminology(**kwargs):
            self.actual_requests += 1
            payload = json.loads(kwargs['messages'][1]['content'].split('候选术语：', 1)[1])
            self.assertEqual({x['term'] for x in payload}, {x.term for x in self.candidates[key]})
            return self.response(json.dumps({'translations': {x['term']: '离线受控术语' for x in payload}}, ensure_ascii=False))
        with patch('openai.AsyncOpenAI', return_value=N(chat=N(completions=N(create=terminology)))):
            asyncio.run(translate_glossary(self.candidates[key]))
        translator = SemanticsTranslator(model='deepseek-flash')
        with patch.object(translator, '_get_client', return_value=self.translation_client()):
            translated, metadata = asyncio.run(translator._call_llm_json_batch([
                {'id': 0, 'html': self.samples[key]['html']}]))
            self.assertEqual(translated[0], self.samples[key]['html'])
        plan = self.plans[key]
        if plan:
            def precision(payload):
                self.actual_requests += 1
                parsed = json.loads(payload['messages'][-1]['content'])
                self.assertEqual(parsed['context'], plan.paragraphs[0].context)
                return self.keep_response(payload)
            with LLMPolisher() as polisher, patch.object(LLMPolisher, '_request', side_effect=precision):
                decisions = polisher.review(plan.paragraphs[0])
                self.assertEqual(decisions, {x.id: x.source for x in plan.paragraphs[0].occurrences})

    def test_three_actual_books_four_callers_and_real_text_segment_rescue(self):
        from app.engine.cleaners.semantics_translator import SemanticsTranslator
        from app.infra.llm_usage_ledger import get_ledger
        with self.scope():
            for key in self.sources:
                self.exercise_book(key)
            translator = SemanticsTranslator(model='deepseek-flash')
            with patch.object(translator, '_get_client', return_value=self.translation_client()):
                result, metadata, latency = asyncio.run(translator._translate_text_segments_rescue(
                    self.rescue_sample['html'], 'controlled historical transport probe'))
            self.assertEqual(translator.stats.text_segment_rescue_successes, 1)
        rows = get_ledger(self.engine).requests(self.job_id)
        self.assertEqual(len(rows), self.actual_requests)
        self.assertEqual(len(self.redis.acquired), self.actual_requests)
        self.assertEqual(len(self.redis.adjusted), self.actual_requests)
        self.assertEqual(len({record[1] for record in self.redis.acquired}), 1)
        self.assertEqual(len({record[2] for record in self.redis.acquired}), self.actual_requests)
        self.assertEqual({row['stage'] for row in rows}, {'body', 'book_profile', 'glossary', 'precision_polish', 'rescue'})
        self.assertTrue(all(row['usage_status'] == 'complete' for row in rows))
        self.assertIsNone(self.plans['double-helix'])
        self.assertIsNotNone(self.plans['die-with-zero'])
        self.assertIsNotNone(self.plans['responsibility-and-judgement'])
        print('R12 history governance: ' + json.dumps({'books': 3, 'physical_requests': self.actual_requests,
              'quota_leases': len(self.redis.acquired), 'ledger_rows': len(rows),
              'stages': sorted({row['stage'] for row in rows}), 'source_sample_hashes': self.source_hashes}))

    def test_real_profile_input_budget_rejects_before_paid_transport(self):
        from app.infra.llm_gateway import dispatch_budget_scope, GatewayControlError
        from app.infra.llm_usage_ledger import get_ledger
        def reject(*args, **kwargs):
            raise GatewayControlError('controlled history budget exhausted')
        for key in self.sources:
            with self.subTest(book=key), self.scope(), dispatch_budget_scope(reject):
                with self.assertRaises(GatewayControlError):
                    self.exercise_book(key)
        self.assertEqual(self.actual_requests, 0)
        self.assertEqual(get_ledger(self.engine).requests(self.job_id), [])

    def test_originals_and_previous_artifacts_are_unchanged(self):
        self.assert_history_unchanged()


if __name__ == '__main__':
    unittest.main(verbosity=2)
