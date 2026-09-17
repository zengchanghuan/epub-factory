"""Offline regressions for paid bilingual books: QA, anchors and task limits."""
import os
import asyncio
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from bs4 import BeautifulSoup
from app.domain.translation_qa_service import audit_translated_epub_output
from app.domain.chapter_reduce_service import apply_chunk_results
from app.engine.chunk_extractor import extract_chunks, should_skip_reference_note_block
from app.infra.celery_app import build_celery_app
from app.job_runner import _apply_final_artifact_audit
from app.models import ConversionResult
from app.engine.cleaners.semantics_translator import SemanticsTranslator, _within_request_budget
from billiard.exceptions import SoftTimeLimitExceeded

ENGLISH = ('This paragraph describes how players learn to cooperate with other people '
           'and improve their skills through regular practice. Repeated interaction encourages '
           'them to share information and work together toward common goals.')
CHINESE = '本段说明玩家如何学习与他人合作，并通过经常练习提高技能。'


class TestPaidTranslationRegression(unittest.TestCase):
    def audit(self, body, bilingual=True):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sample.epub'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('chapter.xhtml', f'<html><body>{body}</body></html>')
            return audit_translated_epub_output(path, bilingual=bilingual)

    def test_bilingual_source_does_not_count_as_untranslated(self):
        body = (f'<p><span class="epub-original">{ENGLISH}</span><br/>'
                f'<span class="epub-translated">{CHINESE}</span></p>')
        self.assertEqual(self.audit(body)['status'], 'passed')
        self.assertEqual(self.audit(body, bilingual=False)['status'], 'failed')

    def test_missing_empty_or_untranslated_partner_still_fails(self):
        for translated in ['', '<span class="epub-translated"></span>',
                           f'<span class="epub-translated">{ENGLISH}</span>']:
            with self.subTest(translated=translated):
                body = f'<p><span class="epub-original">{ENGLISH}</span>{translated}</p>'
                self.assertEqual(self.audit(body)['status'], 'failed')
        self.assertEqual(self.audit(f'<p>{ENGLISH}</p>')['status'], 'failed')

    def test_nested_source_markup_does_not_crash_audit(self):
        body = (f'<p><span class="epub-original"><span class="epub-original">{ENGLISH}'
                f'</span></span><span class="epub-translated">{CHINESE}</span></p>')
        self.assertEqual(self.audit(body)['status'], 'passed')

    def test_bilingual_inline_ids_remain_unique_and_links_resolve(self):
        raw = b'<html><body><p>Hello<a id="note1" href="#page1">1</a><span id="page1">page</span></p></body></html>'
        chunk = extract_chunks(raw, 'ch')[0]
        result = SimpleNamespace(locator=chunk.locator, sequence=0, chunk_id='ch0',
            translated_html='<p>你好<a id="note1" href="#page1">1</a><span id="page1">页</span></p>')
        output = BeautifulSoup(apply_chunk_results(raw, [result], bilingual=True), 'html.parser')
        ids = [node['id'] for node in output.find_all(id=True)]
        self.assertEqual(sorted(ids), ['note1', 'page1'])
        self.assertEqual(len(output.select('.epub-original')), 1)
        self.assertEqual(len(output.select('.epub-translated')), 1)
        for link in output.find_all(href=True):
            self.assertIn(link['href'][1:], ids)

    def test_citation_initials_do_not_trigger_translation(self):
        citation = ('J. A. Example et al., “Learning and Cooperation in Games,” '
                    'Research 501 (2013), doi:10.1234/example.123.')
        p = BeautifulSoup(f'<p class="endnote">{citation}</p>', 'html.parser').p
        self.assertTrue(should_skip_reference_note_block(p))
        p.string = (citation + ' This study has limitations. '
                    'Its results should not be generalized to all players.')
        self.assertFalse(should_skip_reference_note_block(p))
        plain = BeautifulSoup(f'<p>{citation}</p>', 'html.parser').p
        self.assertFalse(should_skip_reference_note_block(plain))

    def test_previous_failure_is_not_replaced_by_artifact_scan(self):
        result = ConversionResult(validation_passed=False,
            message='EPUB validation failed: duplicate ID', error_code='EPUB_VALIDATION_FAILED')
        with patch('app.job_runner.audit_translated_epub_output') as audit:
            _apply_final_artifact_audit(SimpleNamespace(enable_translation=True), result, Path('/missing.epub'))
        audit.assert_not_called()
        self.assertEqual(result.error_code, 'EPUB_VALIDATION_FAILED')
        self.assertIn('duplicate ID', result.message)

    def test_job_passes_bilingual_mode_to_auditor(self):
        with patch('app.job_runner.audit_translated_epub_output', return_value={'status': 'passed'}) as audit:
            _apply_final_artifact_audit(SimpleNamespace(enable_translation=True, bilingual=True),
                                        ConversionResult(), Path('/sample.epub'))
        self.assertTrue(audit.call_args.kwargs['bilingual'])

    def test_timeout_bonus_and_retry_budget_count_actual_requests(self):
        translator = SemanticsTranslator(target_lang="zh-CN")
        translator.max_retries = 4
        translator.timeout_extra_retries = 2
        request = AsyncMock(side_effect=TimeoutError("simulated timeout"))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        payload = [{"id": 0, "html": ENGLISH}]
        with patch.object(translator, '_get_client', return_value=client), \
             patch.object(translator, '_candidate_routes', return_value=[('https://mock.invalid', 'mock')]), \
             patch('app.engine.cleaners.semantics_translator.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(TimeoutError):
                asyncio.run(translator._call_llm_json_batch(payload))
            self.assertEqual(request.await_count, 6)
            self.assertEqual(translator.stats.api_calls, 6)
            request.reset_mock()
            with self.assertRaises(TimeoutError) as failure:
                asyncio.run(_within_request_budget(1, lambda: translator._call_llm_json_batch(payload)))
            self.assertEqual(request.await_count, 1)
            self.assertEqual(failure.exception.translation_request_attempts, 1)
            request.reset_mock()
            translator.chunk_retry_budget = 6
            result = asyncio.run(translator.translate_many_chunks_async(
                ['<p>' + ENGLISH + '</p>'], prior_retry_counts=[5]))
            self.assertEqual(request.await_count, 1)
            self.assertEqual(result[0].retry_count, 6)
            self.assertIsNotNone(result[0].error)
            request.side_effect = SoftTimeLimitExceeded()
            with self.assertRaises(SoftTimeLimitExceeded):
                asyncio.run(translator.translate_single_chunk_async('<p>' + ENGLISH + '</p>'))

    def test_whole_book_has_separate_bounded_time_limit(self):
        with patch.dict(os.environ, {}, clear=True):
            app = build_celery_app()
            self.assertEqual(app.conf.task_soft_time_limit, 1500)
            limits = app.conf.task_annotations['jobs.run_conversion']
            self.assertEqual(limits['soft_time_limit'], 7200)
            self.assertGreater(limits['time_limit'], limits['soft_time_limit'])
        with patch.dict(os.environ, {'EPUB_BOOK_SOFT_TIME_LIMIT': '3600', 'EPUB_BOOK_TIME_LIMIT': '3900'}):
            self.assertEqual(build_celery_app().conf.task_annotations['jobs.run_conversion']['soft_time_limit'], 3600)
        with patch.dict(os.environ, {'EPUB_BOOK_SOFT_TIME_LIMIT': '3600', 'EPUB_BOOK_TIME_LIMIT': '3000'}):
            with self.assertRaises(ValueError):
                build_celery_app()


if __name__ == '__main__':
    unittest.main()
