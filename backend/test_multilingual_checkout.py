"""Offline contracts for five-language checkout and translation routing."""
import unittest
from unittest.mock import patch, AsyncMock
import asyncio
from app import main
from app.domain.translation_scripts import LANGUAGES, has_foreign_letters
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.domain.translation_checkpoints import book_resume_key
import test_paypal_checkout as fixtures


class MultilingualTests(unittest.TestCase):
    setUp = fixtures.PayPalTests.setUp
    create = fixtures.PayPalTests.create
    created = fixtures.PayPalTests.created

    def test_twenty_language_pairs_freeze_source_target_and_usd_quote(self):
        with patch.object(main, '_prepare_translation_request', return_value=(
                {'price_cny':'14.01','total_chars':120000}, None, None, [])):
            for source in LANGUAGES:
                for target in LANGUAGES:
                    if source == target:
                        continue
                    with self.subTest(source=source,target=target):
                        job=self.created(enable_translation='true',source_lang=source,target_lang=target)
                        self.assertEqual(job.target_lang,target)
                        self.assertEqual(job.translation_stats['source_lang'],source)
                        self.assertEqual(job.expected_amount,'2.41')
                        detail=self.client.get('/api/v2/jobs/'+job.id).json()
                        self.assertEqual(detail['source_lang'],source)
                        self.assertEqual(detail['target_lang'],target)
        self.pay.assert_not_called()
        self.enqueue.assert_not_called()

    def test_txt_twenty_pairs_keep_source_target_and_paypal_quote(self):
        samples={'zh-CN':'这是一本书。','zh-TW':'這是一本書。','en':'This is a book.','ja':'これは本です。','ko':'이것은 책입니다.'}
        with patch.object(main,'_prepare_translation_request',return_value=({'price_cny':'3.99','total_chars':20},None,None,[])):
            for source,text in samples.items():
                for target in LANGUAGES:
                    if source==target:continue
                    job=self.created(filename='book.txt',content=text.encode(),enable_translation='true',source_lang=source,target_lang=target)
                    self.assertEqual(job.target_lang,target)
                    self.assertEqual(job.translation_stats['source_lang'],source)
                    self.assertEqual(job.expected_amount,'1.49')

    def test_invalid_language_is_rejected_before_quote_or_payment(self):
        with patch.object(main,'_prepare_translation_request') as prepare:
            for fields in ({'target_lang':'arbitrary prompt'}, {'source_lang':'invalid'}, {'source_lang':'en','target_lang':'en'}):
                self.assertEqual(self.create(enable_translation='true',**fields).status_code,400)
            prepare.assert_not_called()
        self.pay.assert_not_called()

    def test_scripts_and_explicit_source_do_not_skip_han_only_translation(self):
        examples={'zh-CN':'这是简体中文内容。','zh-TW':'這是繁體中文內容。','en':'This is English content.',
                  'ja':'日本語の文章です。','ko':'한국어 문장입니다.'}
        for source,content in examples.items():
            for target in LANGUAGES:
                if source!=target:self.assertTrue(has_foreign_letters(content,target,source),(source,target))
        self.assertTrue(has_foreign_letters('这是简体中文','zh-TW'))
        self.assertTrue(has_foreign_letters('這是繁體中文','zh-CN'))
        self.assertTrue(has_foreign_letters('中文内容','ja'))
        self.assertFalse(has_foreign_letters('12345 + =','en','zh-CN'))

    def test_prompts_and_cache_are_bound_to_target_and_source(self):
        for target in LANGUAGES:
            translator=SemanticsTranslator(target_lang=target)
            translator.source_lang='ja'
            prompt=translator._build_system_prompt()
            self.assertIn(target,prompt)
            if target in {'en','ja','ko'}:
                self.assertNotIn('可靠中文译名',prompt)
                self.assertNotIn('按中文习惯',prompt)
                edited=translator._language_prompt('你是中文图书的章节责任编辑。中文表达自然。')
                self.assertNotIn('你是中文图书',edited)
            self.assertTrue(translator._should_translate('日本漢字内容'))

    def test_all_pairs_process_chunk_with_mock_model(self):
        examples={"zh-CN":"这本书讲述了一个关于旅行和友谊的故事。", "zh-TW":"這本書講述了一個關於旅行和友誼的故事。",
                  "en":"This book tells a story about travel and friendship.",
                  "ja":"この本は旅と友情についての物語です。", "ko":"이 책은 여행과 우정에 관한 이야기입니다."}
        for source in LANGUAGES:
            for target in LANGUAGES:
                if source==target: continue
                with self.subTest(source=source,target=target):
                    translator=SemanticsTranslator(target_lang=target)
                    translator.source_lang=source
                    model=AsyncMock(return_value=({0:examples[target]}, {}))
                    with patch.object(translator,'_cache_get',return_value=None), patch.object(translator,'_cache_set'), patch.object(translator,'_call_llm_json_batch',model):
                        result=asyncio.run(translator.translate_single_chunk_async('<p>'+examples[source]+'</p>'))
                    self.assertIsNone(result.error)
                    self.assertEqual(result.translated_html,examples[target])
                    model.assert_awaited()

    def test_non_chinese_target_rejects_long_unchanged_source(self):
        for target in ['en','ja','ko']:
            translator=SemanticsTranslator(target_lang=target)
            translator.source_lang='zh-CN'
            original='这是一段需要完整翻译的中文正文。'*5
            self.assertTrue(translator._looks_untranslated(original,original))
            self.assertFalse(translator._looks_untranslated('DNA','DNA'))

    def test_retry_preserves_source_language(self):
        from app.domain.translation_attempt import restarted_translation_stats
        self.assertEqual(restarted_translation_stats({"source_lang":"ja"}, attempt_id="retry")["source_lang"], "ja")

    def test_source_selection_invalidates_book_checkpoint(self):
        with patch.object(main,'_prepare_translation_request',return_value=({'price_cny':'7.00'},None,None,[])):
            job=self.created(enable_translation='true',source_lang='ja',target_lang='en')
        first=book_resume_key(job,{'chapters':[]})
        job.translation_stats['source_lang']='ko'
        self.assertNotEqual(first,book_resume_key(job,{'chapters':[]}))

if __name__=='__main__':unittest.main()
