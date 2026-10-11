"""Empty visible prose must never be counted as a successful translation."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.engine.cleaners.semantics_translator import SemanticsTranslator, SingleChunkResult
from app.engine.translation_cache import TranslationCache


class EmptyTranslationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'OPENAI_API_KEY': 'offline', 'OPENAI_BASE_URL': 'https://offline.invalid',
            'OPENAI_MODEL': 'deepseek-flash', 'OPENAI_MAX_RETRIES': '1',
            'EPUB_TRANSLATION_QUALITY_RETRIES': '1',
            'EPUB_TRANSLATION_CHUNK_RETRY_BUDGET': '1',
            'EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE': '0',
            'EPUB_TRANSLATION_PROACTIVE_QUALITY_MODEL_ENABLED': '0',
        })
        self.env.start(); self.addCleanup(self.env.stop)
        cache = TranslationCache(str(Path(self.tmp.name) / 'cache.db'))
        self.cache_patch = patch('app.engine.cleaners.semantics_translator.TranslationCache', return_value=cache)
        self.cache_patch.start(); self.addCleanup(self.cache_patch.stop)
        self.t = SemanticsTranslator(model='deepseek-flash', quality_fallback_model='')
        self.cache = cache

    def response(self, value):
        async def call(payload, **kwargs):
            return {item['id']: value for item in payload}, {'attempts': 1}
        self.t._call_llm_json_batch = call

    def test_empty_visible_variants_are_rejected_in_all_languages(self):
        for lang in ('zh-CN', 'en', 'fr'):
            self.t.target_lang = lang
            for candidate in ('', ' \n\t', '&nbsp;&#160;\u3000', '<span></span>',
                              '<p><span> \n&nbsp;</span></p>', '<!-- explanation -->',
                              '<script>not prose</script>', '<style>not prose</style>'):
                with self.subTest(lang=lang, candidate=candidate):
                    self.assertEqual(self.t._invalid_translation_reason('<p>Actual source prose.</p>', candidate),
                                     '模型返回空译文')

    def test_images_math_and_source_placeholders_are_not_false_positives(self):
        for source in ('<img src="cover.jpg" alt="Cover"/>', '<svg><text>Image text</text></svg>',
                       '<math><mi>x</mi></math>', '<p><span> </span></p>'):
            self.assertIsNone(self.t._invalid_translation_reason(source, source))
        source = '<img src="cover.jpg"/>Actual caption.'
        self.assertEqual(self.t._invalid_translation_reason(source, '<img src="cover.jpg"/>'), '模型返回空译文')
        self.assertIsNone(self.t._invalid_translation_reason(source, '<img src="cover.jpg"/>实际图注。'))

    def test_bad_cache_is_rejected_on_read_and_write(self):
        source = '<span>Actual prose.</span>'
        for bad in (' \n ', '<span>&nbsp;</span>'):
            self.cache.set(source, bad, self.t._cache_key_for_context())
            self.assertIsNone(self.t._cache_get(source))
            with patch.object(self.cache, 'set') as write:
                self.t._cache_set(source, bad)
                write.assert_not_called()

    def test_many_chunks_exhausted_retries_preserve_source_and_do_not_cache(self):
        for bad in (' \n ', '<span> </span>'):
            self.response(bad)
            source = '<p><span>Actual prose.</span></p>'
            with patch.object(self.cache, 'set') as write:
                before = self.t.stats.translated_chunks
                result = asyncio.run(self.t.translate_many_chunks_async([source]))[0]
                self.assertTrue(result.error)
                self.assertIn('Actual prose.', result.translated_html)
                self.assertEqual(self.t.stats.translated_chunks, before)
                write.assert_not_called()

    def test_repair_succeeds_without_retranslating_good_sibling(self):
        calls = []
        async def call(payload, **kwargs):
            calls.append(payload)
            if len(calls) == 1:
                return {0: '<span> </span>', 1: '正常译文。'}, {'attempts': 1}
            return {0: '<span>修复译文。</span>'}, {'attempts': 1}
        self.t._call_llm_json_batch = call
        with patch.object(self.cache, 'set', wraps=self.cache.set) as write:
            results = asyncio.run(self.t.translate_many_chunks_async([
                '<p><span>Broken response.</span></p>', '<p>Healthy sibling.</p>']))
            self.assertTrue(all(r.error is None for r in results))
            self.assertEqual([len(p) for p in calls], [2, 1])
            self.assertEqual(self.t.stats.translated_chunks, 2)
            self.assertEqual(write.call_count, 2)
            self.assertTrue(all(self.t._visible_text(c.args[1]) for c in write.call_args_list))

    def test_single_chunk_never_returns_success_for_empty_markup(self):
        for bad in (' \n ', '<span></span>'):
            self.response(bad)
            with patch.object(self.cache, 'set') as write:
                result = asyncio.run(self.t.translate_single_chunk_async('<p><span>Actual prose.</span></p>'))
                self.assertTrue(result.error)
                self.assertIn('Actual prose.', result.translated_html)
                write.assert_not_called()

    def test_legacy_helpers_raise_before_success_or_cache(self):
        self.response(' \n ')
        for method, arg in ((self.t._translate_batch, ['Actual prose.']),
                            (self.t._translate_html_chunk, 'Actual prose.')):
            with self.assertRaisesRegex(ValueError, '空译文'), patch.object(self.cache, 'set') as write:
                asyncio.run(method(arg))
            write.assert_not_called()
        self.assertEqual(self.t.stats.translated_chunks, 0)

    def test_empty_checkpoint_is_not_reused(self):
        self.response('恢复的译文。')
        checkpoint = SingleChunkResult(' \n ', False, None, None, 0, 0, 0, None)
        result = asyncio.run(self.t.translate_many_chunks_async(
            ['<p>Actual prose.</p>'], checkpoint_results=[checkpoint]))[0]
        self.assertIsNone(result.error)
        self.assertEqual(result.translated_html, '恢复的译文。')

    def test_text_node_rescue_rejects_one_empty_segment_in_otherwise_nonempty_block(self):
        async def call(payload, **kwargs):
            return {item['id']: (' \n ' if i == 0 else '其余文本。') for i, item in enumerate(payload)}, {}
        self.t._call_llm_json_batch = call
        with patch.dict(os.environ, {'EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE': '1'}), self.assertRaisesRegex(ValueError, 'empty translation'):
            asyncio.run(self.t._translate_text_segments_rescue('Before <em>important text</em> after.', 'test'))


if __name__ == '__main__':
    unittest.main()
