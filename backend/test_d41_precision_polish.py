"""R3 deterministic safety/usage tests; no provider network calls."""
import json
import os
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from lxml import etree
from sqlalchemy import create_engine, select

with patch('dotenv.load_dotenv', return_value=False):
    from app.domain import precision_polish_service as service
    from app.cancellation import JobCancelled
    from app.engine.cleaners import llm_polish as module
    from app.infra.llm_usage_ledger import AccountingError, UsageRequest, usage_scope


def html(body):
    return ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Test</title></head><body>'
            + body + '</body></html>').encode()


def book(path, documents):
    names = list(documents)
    items = ''.join(f'<item id="d{i}" href="{name}" media-type="application/xhtml+xml"/>' for i, name in enumerate(names))
    spine = ''.join(f'<itemref idref="d{i}"/>' for i in range(len(names)))
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', b'application/epub+zip')
        archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
        archive.writestr('OPS/book.opf', f'<package><manifest>{items}</manifest><spine>{spine}</spine></package>')
        for name, content in documents.items():
            archive.writestr('OPS/' + name, content)
        archive.writestr('OPS/image.png', b'UNCHANGED_IMAGE')
        archive.writestr('OPS/toc.ncx', b'UNCHANGED_NAV')


def envelope(payload, replacements=None, *, transform=None, identity='response-1'):
    request = json.loads(payload['messages'][-1]['content'])
    replacements = replacements or {}
    decisions = []
    for occurrence in request['occurrences']:
        source = occurrence['source']
        new = replacements.get(source, source)
        decisions.append({'id': occurrence['id'], 'source': source,
                          'action': 'keep' if new == source else 'replace', 'replacement': new})
    if transform:
        decisions = transform(decisions)
    return {'id': identity, 'model': payload['model'],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130,
                      'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 100},
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'decisions': decisions}, ensure_ascii=False)}}]}


class PrecisionPolishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = create_engine('sqlite:///' + str(self.root / 'ledger.db'))
        self.addCleanup(self.engine.dispose)
        environment = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key', 'DEEPSEEK_BASE_URL': 'https://api.deepseek.com/v1',
                                             'DEEPSEEK_MODEL': 'deepseek-flash', 'L4_MAX_TOKENS_PER_BOOK': '2000000',
                                             'L4_MAX_INPUT_BYTES_PER_PARA': '8192',
                                             'L4_MAX_TOKENS_PER_PARA': '4096'})
        environment.start(); self.addCleanup(environment.stop)
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('No model/network calls'))
        network.start(); self.addCleanup(network.stop)
        gate = patch.object(service, 'validate_epub', return_value=SimpleNamespace(passed=True, message='Unit QA stub'))
        self.gate = gate.start(); self.addCleanup(gate.stop)
        self.source, self.output = self.root / 'source.epub', self.root / 'polished.epub'
        self.requests = []

    def factory(self, replacements=None, transform=None, handler=None):
        test = self
        class Controlled(module.LLMPolisher):
            def _request(self, payload):
                test.requests.append(payload)
                if handler:
                    return handler(payload)
                return envelope(payload, replacements, transform=transform, identity=f'response-{len(test.requests)}')
        return Controlled

    def run_book(self, documents, **kwargs):
        book(self.source, documents)
        with usage_scope('test-job', 'precision-attempt', engine=self.engine):
            return service.run_precision_polish(self.source, self.output, **kwargs)

    def rows(self):
        with self.engine.connect() as connection:
            return connection.execute(select(UsageRequest.__table__)).mappings().all()

    def test_no_candidates_does_not_construct_model_and_all_members_unchanged(self):
        stats = self.run_book({'part.xhtml': html('<p>Hello world.</p>')},
                              polisher_factory=lambda: self.fail('No-candidate book constructed a model'))
        self.assertEqual(stats['status'], 'no_candidates')
        self.assertEqual(stats['api_calls'], 0)
        with zipfile.ZipFile(self.source) as before, zipfile.ZipFile(self.output) as after:
            self.assertEqual(before.namelist(), after.namelist())
            for name in before.namelist():
                self.assertEqual(before.read(name), after.read(name))
        self.assertEqual(self.rows(), [])

    def test_inspection_matches_both_traditional_and_simplified_without_requests(self):
        book(self.source, {'one.xhtml': html('<p>服務生骑機車。</p><p>服务生骑机车。</p>')})
        result = service.inspect_precision_polish_source(self.source)
        self.assertEqual(result['candidates'], 2)
        self.assertEqual(result['documents_scanned'], 1)
        self.assertGreater(result['char_count'], 0)
        self.assertEqual(self.requests, [])

    def test_inspection_excludes_risks_already_resolved_by_selected_dictionary(self):
        content = html('<p>我在超商買東西。</p>')
        book(self.source, {'one.xhtml': content})
        original = self.source.read_bytes()
        raw_count = module.plan_document(content).char_count
        default = service.inspect_precision_polish_source(self.source)
        no_dictionary = service.inspect_precision_polish_source(self.source, lexicon_domains=[])
        tech_only = service.inspect_precision_polish_source(self.source, lexicon_domains=['tech'])
        self.assertEqual(default['candidates'], 0)
        self.assertEqual(no_dictionary['candidates'], 1)
        self.assertEqual(tech_only['candidates'], 1)
        self.assertEqual({default['char_count'], no_dictionary['char_count'], tech_only['char_count']}, {raw_count})
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.requests, [])

    def test_quote_numeric_entities_match_literal_text_and_real_ebooklib_boundary(self):
        from ebooklib import epub
        from app.engine.cleaners.cjk_normalizer import CjkNormalizer
        spellings = ['超商', '&#x8d85;&#x5546;', '&#36229;&#21830;']
        inspections = []
        for spelling in spellings:
            with self.subTest(spelling=spelling):
                content = html('<p>我在' + spelling + '買東西。</p>')
                book(self.source, {'one.xhtml': content})
                original = self.source.read_bytes()
                inspection = service.inspect_precision_polish_source(self.source)
                inspections.append(inspection)
                actual_item = epub.EpubHtml(file_name='one.xhtml', content=content)
                actual_book = epub.EpubBook()
                actual_book.add_item(actual_item)
                actual = CjkNormalizer(output_mode='simplified').process(actual_item.get_content(), 9)
                self.assertEqual(inspection['candidates'], len(module.plan_document(actual).paragraphs))
                self.assertEqual(inspection['candidates'], 0)
                self.assertEqual(self.source.read_bytes(), original)
                self.assertEqual(service.inspect_precision_polish_source(self.source, lexicon_domains=[])['candidates'], 1)
        self.assertTrue(all(result == inspections[0] for result in inspections))
        self.assertEqual(len({module.calculate_polish_price(result['char_count']) for result in inspections}), 1)
        self.assertEqual(self.requests, [])

    def test_inspection_uses_exact_cjk_configuration_and_execution_does_not_repeat_it(self):
        from app.engine.cleaners.cjk_normalizer import CjkNormalizer
        content = html('<p>超商賣機車配件。</p>')
        book(self.source, {'one.xhtml': content})
        configurations = [dict(traditional_variant='auto', lexicon_domains=[], enable_proper_noun=False),
                          dict(traditional_variant='tw', lexicon_domains=['general'], enable_proper_noun=True),
                          dict(traditional_variant='hk', lexicon_domains=['tech'], enable_proper_noun=False)]
        for configuration in configurations:
            with self.subTest(configuration=configuration):
                actual = CjkNormalizer(output_mode='simplified', **configuration).process(content, 9)
                expected = module.plan_document(actual)
                inspection = service.inspect_precision_polish_source(self.source, **configuration)
                self.assertEqual(inspection['candidates'], len(expected.paragraphs))
        # Production service input has already passed conversion; it must never
        # apply a second, possibly different default dictionary pass.
        with patch.object(CjkNormalizer, 'process', side_effect=AssertionError('No second CJK pass')):
            self.run_book({'one.xhtml': html('<p>超商</p>')}, polisher_factory=self.factory())

    def test_malformed_package_and_image_only_source_rejected(self):
        self.source.write_bytes(b'not a zip')
        with self.assertRaises(module.PrecisionPolishError):
            service.inspect_precision_polish_source(self.source)
        book(self.source, {'one.xhtml': html('<p><img src="image.png" alt=""/></p>')})
        with self.assertRaises(module.PrecisionPolishError) as caught:
            service.inspect_precision_polish_source(self.source)
        self.assertEqual(caught.exception.reason, 'no_body_text')

    def test_variable_length_multiple_paragraphs_preserve_markup_numbers_links_and_assets(self):
        content = html('<p id="one">机车<em>搞</em>混<a href="#two">机车</a> 1938。</p>'
                       '<p id="two">机车服务<span>窩心</span>。</p>')
        stats = self.run_book({'one.xhtml': content}, polisher_factory=self.factory({'机车': '摩托车', '搞': '弄', '窩心': '暖心'}))
        self.assertEqual((stats['reviewed'], stats['changed'], stats['api_calls']), (2, 2, 2))
        with zipfile.ZipFile(self.output) as archive:
            out = etree.fromstring(archive.read('OPS/one.xhtml'))
            self.assertEqual(out.xpath('//*[local-name()="p"]/@id'), ['one', 'two'])
            self.assertEqual(out.xpath('//*[local-name()="a"]/@href'), ['#two'])
            self.assertEqual(out.xpath('string(//*[local-name()="a"])'), '机车')
            self.assertIn('摩托车弄混机车 1938', ''.join(out.itertext()))
            self.assertIn('摩托车服务暖心', ''.join(out.itertext()))
            self.assertEqual(archive.read('OPS/image.png'), b'UNCHANGED_IMAGE')
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r['stage'] == 'precision_polish' and r['total_tokens'] == 130 for r in self.rows()))
        self.assertNotIn('cost_cny', stats)

    def test_legitimate_noop_is_reviewed_not_skipped_and_callbacks_contain_no_text(self):
        snapshots = []
        stats = self.run_book({'one.xhtml': html('<p>一九三八年超过上限，服务照常。</p>')},
                              polisher_factory=self.factory(), stats_callback=snapshots.append)
        self.assertEqual((stats['status'], stats['reviewed'], stats['changed'], stats['unchanged']), ('completed', 1, 0, 1))
        self.assertNotIn('一九三八', json.dumps(snapshots, ensure_ascii=False))
        with zipfile.ZipFile(self.output) as archive, zipfile.ZipFile(self.source) as source:
            self.assertEqual(archive.read('OPS/one.xhtml'), source.read('OPS/one.xhtml'))

    def test_protected_context_and_foreign_markup_are_not_editable(self):
        plan = module.plan_document(html('<p>机车<a href="x">服务</a><code>搞</code><span translate="no">窩心</span>'
                                         '<svg xmlns="http://www.w3.org/2000/svg"><text>超</text></svg>三八</p>'))
        self.assertEqual([o.source for o in plan.paragraphs[0].occurrences], ['机车', '三八'])
        self.assertFalse(plan.paragraphs[0].occurrences[-1].editable)

    def test_split_risky_word_rejected_before_charging(self):
        with self.assertRaises(module.PrecisionPolishError) as caught:
            module.plan_document(html('<p>服<em>务</em>。</p>'))
        self.assertEqual(caught.exception.reason, 'unsafe_candidate')

    def test_known_proper_noun_protection_spans_inline_nodes(self):
        with patch.object(module, '_load_terms', return_value=({'超': 'risk'}, {'超级集团'})):
            plan = module.plan_document(html('<p><em>超</em>级集团</p>'))
        occurrence = plan.paragraphs[0].occurrences[0]
        self.assertFalse(occurrence.editable)
        with self.assertRaises(module.PrecisionPolishError):
            module._validate_decisions(plan.paragraphs[0], {'decisions': [
                {'id': '0', 'source': '超', 'action': 'replace', 'replacement': '非常'}]})

    def test_duplicate_json_fields_and_nonstring_action_are_rejected(self):
        def ambiguous(payload):
            result = envelope(payload)
            result['choices'][0]['message']['content'] = '{"decisions": [], "decisions": []}'
            return result
        with self.assertRaises(module.PrecisionPolishError):
            self.run_book({'one.xhtml': html('<p>服务</p>')}, polisher_factory=self.factory(handler=ambiguous))
        with self.assertRaises(module.PrecisionPolishError):
            self.run_book({'one.xhtml': html('<p>服务</p>')},
                          polisher_factory=self.factory(transform=lambda d: [dict(d[0], action=[])]))

    def test_missing_duplicate_fabricated_or_wrong_source_decisions_fail_closed(self):
        transforms = [lambda d: [], lambda d: d + d, lambda d: [dict(d[0], id='invented')],
                      lambda d: [dict(d[0], source='different')]]
        for number, transform in enumerate(transforms):
            with self.subTest(number=number), self.assertRaises(module.PrecisionPolishError):
                self.run_book({'one.xhtml': html('<p>机车</p>')}, polisher_factory=self.factory(transform=transform))
            self.assertFalse(self.output.exists())

    def test_html_numeric_and_explanation_replacements_fail_closed(self):
        for replacement in ['<p>新内容</p>', '1939', '新词。解释', '', '新' * 25]:
            with self.subTest(replacement=replacement), self.assertRaises(module.PrecisionPolishError):
                self.run_book({'one.xhtml': html('<p>机车</p>')}, polisher_factory=self.factory({'机车': replacement}))
            self.assertFalse(self.output.exists())
        with self.assertRaises(module.PrecisionPolishError):
            self.run_book({'one.xhtml': html('<p>一九三八年</p>')}, polisher_factory=self.factory({'三八': '三九'}))

    def test_book_budget_shared_across_documents_and_no_partial_output(self):
        with patch.dict(os.environ, {'L4_MAX_TOKENS_PER_BOOK': '1'}):
            with self.assertRaises(module.PrecisionPolishError) as caught:
                self.run_book({'one.xhtml': html('<p>服务</p>'), 'two.xhtml': html('<p>服务</p>')}, polisher_factory=self.factory())
        self.assertEqual(caught.exception.reason, 'budget_exceeded')
        self.assertEqual(self.requests, [])
        self.assertFalse(self.output.exists())
        with self.factory()() as polisher:
            with usage_scope('shared', 'attempt', engine=self.engine):
                polisher.polish_html(html('<p>服务</p>').decode())
                used = polisher._budget_used
                polisher.book_limit = used
                with self.assertRaises(module.PrecisionPolishError):
                    polisher.polish_html(html('<p>服务</p>').decode())
                self.assertEqual(polisher.stats.api_calls, 1)

    def test_input_byte_guard_is_separate_from_output_and_matches_inspection(self):
        content = html('<p>' + '上下文' * 350 + '服务</p>')
        plan = module.plan_document(content)
        _, size = module._review_payload(plan.paragraphs[0], 'deepseek-flash', 1024)
        self.assertGreater(size, 4096)
        self.assertLess(size, 8192)
        book(self.source, {'one.xhtml': content})
        self.assertEqual(service.inspect_precision_polish_source(self.source)['candidates'], 1)
        stats = self.run_book({'one.xhtml': content}, polisher_factory=self.factory())
        self.assertEqual(stats['reviewed'], 1)
        self.assertEqual(self.requests[0]['max_tokens'], 1024)
        self.output.unlink()
        with patch.dict(os.environ, {'L4_MAX_INPUT_BYTES_PER_PARA': str(size - 1)}):
            with self.assertRaises(module.PrecisionPolishError):
                service.inspect_precision_polish_source(self.source)
            self.requests.clear()
            with self.assertRaises(module.PrecisionPolishError):
                self.run_book({'one.xhtml': content}, polisher_factory=self.factory())
            self.assertEqual(self.requests, [])

    def test_missing_usage_stops_without_fabricating_zero_cost(self):
        def missing(payload):
            response = envelope(payload)
            del response['usage']
            return response
        with self.assertRaises(module.PrecisionPolishError) as caught:
            self.run_book({'one.xhtml': html('<p>服务</p><p>服务</p>')}, polisher_factory=self.factory(handler=missing))
        self.assertEqual(caught.exception.reason, 'usage_unavailable')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.rows()[0]['usage_status'], 'missing')
        self.assertIsNone(self.rows()[0]['calculated_cost'])
        self.assertFalse(self.output.exists())

    def test_accounting_error_propagates_without_retry_or_output(self):
        with patch('app.infra.llm_usage_ledger._finish', side_effect=AccountingError('ledger unavailable')):
            with self.assertRaises(AccountingError):
                self.run_book({'one.xhtml': html('<p>服务</p><p>服务</p>')}, polisher_factory=self.factory())
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.output.exists())

    def test_missing_ledger_scope_stops_before_dispatch(self):
        book(self.source, {'one.xhtml': html('<p>服务</p>')})
        with self.assertRaises(AccountingError):
            service.run_precision_polish(self.source, self.output, polisher_factory=self.factory())
        self.assertEqual(self.requests, [])
        self.assertFalse(self.output.exists())

    def test_http_status_failure_is_recorded_as_error_and_permanent_not_retried(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(401, json={'error': {'message': 'not authorized'}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            factory = lambda: module.LLMPolisher(client=client)
            with self.assertRaises(module.PrecisionPolishError):
                self.run_book({'one.xhtml': html('<p>服务</p>')}, polisher_factory=factory)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.rows()[0]['request_status'], 'error')
        self.assertEqual(self.rows()[0]['http_status'], 401)

    def test_transient_retry_uses_same_client_and_records_both_attempts(self):
        calls = []
        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503, json={'error': 'temporary'})
            return httpx.Response(200, json=envelope(json.loads(request.content)))
        with httpx.Client(transport=httpx.MockTransport(handle)) as client, patch.object(module.time, 'sleep'):
            stats = self.run_book({'one.xhtml': html('<p>服务</p>')}, polisher_factory=lambda: module.LLMPolisher(client=client))
        self.assertEqual((stats['api_calls'], stats['retries']), (2, 1))
        self.assertEqual(sorted(r['request_status'] for r in self.rows()), ['error', 'response'])

    def test_final_epubcheck_failure_and_cancel_do_not_leave_output(self):
        self.gate.return_value = SimpleNamespace(passed=False, message='Rejected synthetic book')
        with self.assertRaises(module.PrecisionPolishError) as caught:
            self.run_book({'one.xhtml': html('<p>服务</p>')}, polisher_factory=self.factory())
        self.assertEqual(caught.exception.reason, 'validation_failed')
        self.assertFalse(self.output.exists())

    def test_boolean_cancellation_stops_after_inflight_call_and_never_publishes(self):
        with self.assertRaises(JobCancelled):
            self.run_book({'one.xhtml': html('<p>服务</p><p>服务</p>')},
                          polisher_factory=self.factory(), cancel_check=lambda: bool(self.requests))
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.precision-*')), [])
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_book({'one.xhtml': html('<p>服务</p>')}, polisher_factory=self.factory(),
                          cancel_check=lambda: (_ for _ in ()).throw(RuntimeError('cancelled')))
        self.assertFalse(self.output.exists())

    def test_existing_destination_and_source_never_overwritten(self):
        book(self.source, {'one.xhtml': html('<p>服务</p>')})
        original = self.source.read_bytes()
        self.output.write_bytes(b'previous output')
        with self.assertRaises(module.PrecisionPolishError):
            service.run_precision_polish(self.source, self.output)
        with self.assertRaises(module.PrecisionPolishError):
            service.run_precision_polish(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), b'previous output')

    def test_credentials_never_mix_endpoint_groups_and_default_flash(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': '', 'DEEPSEEK_BASE_URL': 'https://wrong.example/v1',
                                     'OPENAI_API_KEY': 'key', 'OPENAI_BASE_URL': '', 'OPENAI_MODEL': ''}):
            with self.assertRaises(module.PrecisionPolishError):
                module.LLMPolisher()
        with patch.dict(os.environ, {'DEEPSEEK_MODEL': ''}):
            with module.LLMPolisher() as polisher:
                self.assertEqual(polisher.model, 'deepseek-flash')

    def test_service_independently_rejects_structural_edits_from_faulty_polisher(self):
        class Bad(module.LLMPolisher):
            def polish_document(self, plan):
                self.stats.reviewed += len(plan.paragraphs)
                self.stats.changed += len(plan.paragraphs)
                return plan.source.replace(b'id="keep"', b'id="deleted"')
        with self.assertRaises(module.PrecisionPolishError) as caught:
            self.run_book({'one.xhtml': html('<p id="keep">服务</p>')}, polisher_factory=Bad)
        self.assertEqual(caught.exception.reason, 'guard_rejected')
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
