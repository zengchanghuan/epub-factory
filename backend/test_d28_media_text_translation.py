"""Regression for image bullets: translate prose, preserve the complete media tree."""
import asyncio
import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from bs4 import BeautifulSoup
from app.engine.chunk_extractor import extract_chunks_with_stats, media_subtrees
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.domain.manifest_service import build_manifest
from app.domain.chapter_reduce_service import apply_chunk_results, get_node_by_locator
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_qa_service import audit_translated_epub_output

PROSE = 'A volunteer firefighter who has not had a single losing month in over 11 years of trading.'
ZH = '一位志愿消防员，在超过11年的交易生涯中，没有任何一个月出现亏损。'
MEDIA = '<svg id="vector"><g><text>Keep every media label unchanged, including these English words.</text></g></svg>'


class MediaTextTranslationTests(unittest.TestCase):
    def translator(self):
        instance = SemanticsTranslator(target_lang='zh-CN')
        instance._cache_get = lambda *args, **kwargs: None
        instance._cache_set = lambda *args, **kwargs: None
        return instance

    def test_extractor_does_not_skip_image_bullet_or_mixed_caption(self):
        raw = (f'<html><body><p id="bullet"><img src="bullet.jpg"/>{PROSE}</p>'
               f'<div class="figure">{MEDIA}The chart describes trading returns.</div>'
               f'<p>{MEDIA}</p><p>Later paragraph.</p></body></html>').encode()
        chunks, stats = extract_chunks_with_stats(raw, 'ch')
        self.assertEqual([c.chunk_id for c in chunks], ['ch_0001', 'ch_0002', 'ch_0004'])
        self.assertEqual(chunks[0].text, PROSE)
        self.assertEqual(chunks[1].text, 'The chart describes trading returns.')
        self.assertEqual([c.translation_strategy for c in chunks], ['text_nodes', 'text_nodes', 'html'])
        self.assertEqual(stats['image_note_chunks_skipped'], 1)
        self.assertEqual(stats['image_caption_chunks'], 1)
        self.assertEqual(stats['structured_note_chunks'], 0)

    def test_only_external_text_is_sent_and_every_media_attribute_is_preserved(self):
        raw = f'<p><img alt="original label" id="bullet" src="bullet.jpg"/>{MEDIA}<!-- Never translate this comment. -->{PROSE}</p>'
        instance = self.translator()
        request = AsyncMock(return_value=({0: ZH}, {'model': 'deepseek-flash'}))
        with patch.object(instance, '_call_llm_json_batch', request):
            # No metadata supplied: even this legacy calling convention is safe.
            result = asyncio.run(instance.translate_many_chunks_async([raw]))[0]
        self.assertIsNone(result.error)
        self.assertEqual(request.call_count, 1)
        payload = request.call_args.args[0]
        self.assertEqual([item['html'] for item in payload], [PROSE])
        self.assertTrue(payload[0]['text_node_rescue'])
        self.assertEqual(media_subtrees(raw), media_subtrees(result.translated_html))
        self.assertIn('Never translate this comment.', result.translated_html)
        self.assertIn(ZH, result.translated_html)
        self.assertFalse(audit_translation_chunk(original_html=raw, translated_html=result.translated_html).likely_untranslated)

    def test_single_chunk_and_legacy_document_both_keep_image_and_prose(self):
        raw = f'<p id="unchanged"><img src="bullet.jpg"/>{PROSE}</p>'
        for legacy in [False, True]:
            with self.subTest(legacy=legacy):
                instance = self.translator()
                with patch.object(instance, '_call_llm_json_batch', AsyncMock(return_value=({0: ZH}, {}))):
                    if legacy:
                        result = asyncio.run(instance.process_async(f'<html><body>{raw}</body></html>'.encode(), 9)).decode()
                    else:
                        result = asyncio.run(instance.translate_single_chunk_async(raw)).translated_html
                self.assertIn(ZH, result)
                self.assertEqual(media_subtrees(result), media_subtrees(raw))
                if legacy: self.assertIn('id="unchanged"', result)

    def test_bad_cache_cannot_change_image_source_or_svg_internal_text(self):
        raw = f'<p><img src="bullet.jpg"/>{MEDIA}{PROSE}</p>'
        instance = self.translator()
        for bad in [raw.replace('bullet.jpg', 'other.jpg').replace(PROSE, ZH),
                    raw.replace('Keep every', 'Changed every').replace(PROSE, ZH)]:
            self.assertFalse(instance._preserves_inline_tags(raw, bad))
            self.assertTrue(audit_translation_chunk(original_html=raw, translated_html=bad).html_tag_mismatch)
        instance._cache_get = lambda *args, **kwargs: raw.replace('bullet.jpg', 'other.jpg').replace(PROSE, ZH)
        with patch.object(instance, '_call_llm_json_batch', AsyncMock(return_value=({0: ZH}, {}))):
            result = asyncio.run(instance.translate_many_chunks_async([raw]))[0]
        self.assertFalse(result.cached)
        self.assertEqual(media_subtrees(result.translated_html), media_subtrees(raw))

    def test_short_unconfirmed_heading_is_not_accepted_from_old_cache(self):
        instance = self.translator()
        instance._cache_get = lambda *args, **kwargs: 'KELVIN CHIU'
        requests = []
        async def request(payload, **kwargs):
            requests.append(payload)
            return {0: '测试音译，仅流程夹具'}, {}
        with patch.object(instance, '_call_llm_json_batch', request):
            result = asyncio.run(instance.translate_many_chunks_async(['<h2>KELVIN CHIU</h2>']))[0]
        self.assertEqual(len(requests), 1)
        self.assertFalse(result.cached)
        self.assertIsNone(result.error)

    def test_image_text_nodes_receive_locked_chapter_strategy_and_static_context(self):
        instance = self.translator()
        requests = []
        async def request(payload, *, preferred_model=None, system_prompt=None):
            requests.append((payload, system_prompt))
            return {0: ZH}, {}
        with patch.object(instance, '_call_llm_json_batch', request):
            result = asyncio.run(instance.translate_many_chunks_async(
                [f'<p><img src="bullet.jpg"/>{PROSE}</p>'],
                contexts=['前章摘要：介绍交易者。'], book_translation_strategy='mirror_fidelity'))[0]
        self.assertIsNone(result.error)
        self.assertIn('镜像级忠实', requests[0][1])
        self.assertIn('前章摘要', requests[0][0][0]['context'])
        self.assertIn('mirror_fidelity', requests[0][0][0]['context'])

    def test_identical_paragraphs_and_parent_containers_have_unique_locators(self):
        raw = b'<html><body><div><p>Yes.</p><p>Yes.</p></div><div><p>Yes.</p><p>Yes.</p></div></body></html>'
        chunks, _ = extract_chunks_with_stats(raw, 'same')
        self.assertEqual(len({c.locator for c in chunks}), 4)
        self.assertEqual([c.chunk_id for c in chunks], [f'same_{i:04d}' for i in range(1, 5)])
        soup = BeautifulSoup(raw, 'html.parser')
        self.assertEqual(len({id(get_node_by_locator(soup, c.locator)) for c in chunks}), 4)
        results = [SimpleNamespace(locator=c.locator, sequence=c.sequence, chunk_id=c.chunk_id,
                                   translated_html=f'第{i}个回答。') for i, c in enumerate(chunks, 1)]
        output = BeautifulSoup(apply_chunk_results(raw, results, bilingual=False), 'html.parser')
        self.assertEqual([p.get_text() for p in output.find_all('p')], [f'第{i}个回答。' for i in range(1, 5)])

    def test_short_english_answers_are_not_exempt_but_acronyms_and_confirmations_are(self):
        for text in ['Yes.', 'Exactly.', 'Yes, exactly.', 'No, not yet.']:
            self.assertTrue(audit_translation_chunk(original_html=f'<p>{text}</p>', translated_html=f'<p>{text}</p>').likely_untranslated)
            self.assertFalse(audit_translation_chunk(original_html=f'<p>{text}</p>', translated_html=f'<p>{text}</p>', preserved_terms=[text]).likely_untranslated)
        self.assertFalse(audit_translation_chunk(original_html='<p>NO</p>', translated_html='<p>NO</p>').likely_untranslated)

    def test_final_gate_catches_untranslated_prose_but_not_svg_internal_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'book.epub'
            def audit(body):
                with zipfile.ZipFile(path, 'w') as z: z.writestr('body.xhtml', f'<html><body>{body}</body></html>')
                return audit_translated_epub_output(path)
            result = audit(f'<p><img src="bullet.jpg"/>{PROSE}</p>')
            self.assertEqual(result['residual_blocks'], 1)
            self.assertEqual(result['checked_text_blocks'], 1)
            self.assertEqual(audit(f'<p>{MEDIA}{ZH}</p>')['status'], 'passed')
            self.assertEqual(audit(f'<p>{MEDIA}</p>')['checked_text_blocks'], 0)

    @unittest.skipUnless(os.environ.get('EPUB_REGRESSION_BOOK'), 'selected source book not provided')
    def test_real_book_all_sixteen_image_bullets_enter_manifest(self):
        manifest = build_manifest(os.environ['EPUB_REGRESSION_BOOK'], 'image-bullet-regression')
        self.assertTrue(all(len({c['locator'] for c in chapter['chunks']}) == len(chapter['chunks']) for chapter in manifest['chapters']))
        for chapter_id, expected in [('c38', 9), ('cU0', 7)]:
            chapter = next(c for c in manifest['chapters'] if c['chapter_id'] == chapter_id)
            mixed = [c for c in chapter['chunks'] if BeautifulSoup(c['html'], 'html.parser').find('img')]
            self.assertEqual(len(mixed), expected)
            self.assertTrue(all(c['translation_strategy'] == 'text_nodes' for c in mixed))


if __name__ == '__main__': unittest.main()
