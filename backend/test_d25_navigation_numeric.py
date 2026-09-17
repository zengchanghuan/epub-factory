import tempfile
import unittest
import zipfile
from pathlib import Path
from ebooklib import epub
from app.engine.toc_rebuilder import TocRebuilder
from app.domain.translation_numeric_audit import missing_numeric_facts
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_qa_service import audit_translated_epub_output, build_translation_qa_report


class NavigationNumericTests(unittest.TestCase):
    def test_real_book_numeric_equivalents(self):
        for source, target in [('$1 million and $500,000', '100万美元和50万美元'),
                               ('$1 to $5 billion', '10亿到50亿美元'), ('7th–9th', '第7-9局'),
                               ('1,000', '1000'), ('53 million', '5300万'), ('early 20s', '二十出头'),
                               ('128', '一百二十八'), ('-10%', '百分之负十')]:
            self.assertEqual(missing_numeric_facts(source, target), [], (source, target))
        for source, target in [('$5,000 to $1 million', '5,000美元到100万美元'),
                               ('mid-2021', '2021年中期'), ('—2021', '——2021年'),
                               ('20 grand', '2万美元'), ('50k', '5万'),
                               ('10 or 20 million dollars', '1000万或2000万美元'),
                               ('$50–60 million', '5000万–6000万美元')]:
            self.assertEqual(missing_numeric_facts(source, target), [], (source, target))

    def test_changes_and_repeated_numbers_are_not_hidden(self):
        for source, target in [('128', '一百二十七'), ('-10%', '10%'), ('-$10', '10美元'),
                               ('25%', '25'), ('$5 million', '50万美元'), ('$5', '5欧元'),
                               ('3 and 3', '3'), ('1', '一直如此'), ('2024-06-25', '2024-06-26')]:
            self.assertTrue(missing_numeric_facts(source, target), (source, target))
        self.assertTrue(missing_numeric_facts('negative $10,000', '1万美元'))
        self.assertEqual(missing_numeric_facts('negative $10,000', '负1万美元'), [])
        self.assertTrue(missing_numeric_facts('10 to -9', '10到9'))
        self.assertEqual(missing_numeric_facts('10 to -9', '10到-9'), [])
        self.assertEqual(missing_numeric_facts('.5%–2%', '0.5%到2%'), [])
        self.assertTrue(missing_numeric_facts('.5%', '5%'))

    def test_abbreviations_decimals_and_temporal_yet_are_not_missing_relations(self):
        from app.domain.translation_quality_audit import _sentence_count
        self.assertEqual(_sentence_count('Jack D. Smith arrives at 9 a.m. with a 1.5 ratio.'), 1)
        self.assertEqual(_sentence_count('An ominous voice... then silence. A reply.'), 2)
        for source, target in [('I have not found it yet.', '我还没有找到它。'),
                               ('You haven’t found your answer yet.', '你还没有找到答案。'),
                               ("He couldn't take actual trades yet, since he was still training.", '他还不能实际交易，因为仍在培训。'),
                               ('Not only traders but also musicians learn. Thus far it works.', '不仅交易员，音乐家也会学习。到目前为止有效。'),
                               ('It works not only for traders but for musicians.', '它不仅适用于交易员，也适用于音乐家。'),
                               ('Due to the drought, prices rose.', '价格上涨是由干旱引起的。')]:
            self.assertNotIn('critical_markers_missing', audit_translation_chunk(
                original_html=f'<p>{source}</p>', translated_html=f'<p>{target}</p>').flags)
        self.assertIn('critical_markers_missing', audit_translation_chunk(
            original_html='<p>It rose; yet he lost money.</p>', translated_html='<p>它上涨了；他亏钱了。</p>').flags)
        self.assertIn('critical_markers_missing', audit_translation_chunk(
            original_html='<p>He waited because the market was closed.</p>', translated_html='<p>他等待。市场关闭了。</p>').flags)
        self.assertNotIn('numbers_missing', audit_translation_chunk(
            original_html='<p>$500.<sup><a epub:type="noteref" href="#n">1</a></sup></p>',
            translated_html='<p>500美元。<sup><a epub:type="noteref" href="#n">1</a></sup></p>').flags)

    def test_chinese_short_question_is_not_a_truncation(self):
        self.assertNotIn('suspiciously_short_translation', audit_translation_chunk(
            original_html='<p>How old were you when you started trading?</p>',
            translated_html='<p>你开始交易时多大？</p>').flags)
        self.assertIn('suspiciously_short_translation', audit_translation_chunk(
            original_html='<p>This is a long paragraph about books, politics, publishers, translators, and censorship.</p>',
            translated_html='<p>书。</p>').flags)

    def test_short_names_and_common_titles_require_translation_or_confirmation(self):
        for value in ['KELVIN CHIU', 'Dedication', 'Copyright']:
            audit = audit_translation_chunk(original_html=f'<h2>{value}</h2>', translated_html=f'<h2>{value}</h2>')
            self.assertEqual(audit.risk_level, 'fail')
            confirmed = audit_translation_chunk(original_html=f'<h2>{value}</h2>', translated_html=f'<h2>{value}</h2>', preserved_terms=[value])
            self.assertEqual(confirmed.risk_level, 'ok')
        self.assertEqual(audit_translation_chunk(original_html='<h2>DNA</h2>', translated_html='<h2>DNA</h2>').risk_level, 'ok')

    def test_real_book_negation_scope_reversal_is_a_review_signal(self):
        original = '<p>You need the presence of mind and patience to avoid using the strategy for two or three years at a time.</p>'
        bad = '<p>你需要有沉着冷静和耐心，才能避免一次两三年都不使用这个策略。</p>'
        good = '<p>你需要保持清醒并有耐心，才能一次两三年都不使用这个策略。</p>'
        self.assertIn('negation_scope_suspicious', audit_translation_chunk(original_html=original, translated_html=bad).flags)
        self.assertNotIn('negation_scope_suspicious', audit_translation_chunk(original_html=original, translated_html=good).flags)
        self.assertNotIn('negation_scope_suspicious', audit_translation_chunk(original_html='<p>Avoid not using the strategy.</p>', translated_html=bad).flags)

    def test_toc_uses_exact_glossary_and_preserves_hierarchy_and_anchors(self):
        book = epub.EpubBook()
        section = epub.Link('one.xhtml#part', 'Dedication', 'part')
        person = epub.Link('two.xhtml#name', 'KELVIN CHIU', 'person')
        copyright = epub.Link('three.xhtml', 'Copyright', 'copyright')
        book.toc = [(section, [person]), copyright]
        before = [(section.href, section.uid), (person.href, person.uid), (copyright.href, copyright.uid)]
        TocRebuilder().rebuild(book, target_lang='zh-CN', glossary={'Kelvin Chiu': '凯尔文·邱'})
        self.assertEqual([section.title, person.title, copyright.title], ['献词', '凯尔文·邱', '版权'])
        self.assertEqual(before, [(section.href, section.uid), (person.href, person.uid), (copyright.href, copyright.uid)])
        self.assertIs(book.toc[0][1][0], person)
        mixed = epub.Link('four.xhtml', '克里斯蒂安·KULLAMÄGI', 'mixed')
        book.toc = [mixed]
        TocRebuilder().rebuild(book, target_lang='zh-CN', glossary={'Kullamägi': '库拉马吉'})
        self.assertEqual(mixed.title, '克里斯蒂安·库拉马吉')
        self.assertEqual(mixed.href, 'four.xhtml')

    def test_navigation_audit_checks_real_reader_targets_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'book.epub'
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('EPUB/package.opf', '<package><manifest><item properties="nav" href="nav.xhtml"/></manifest></package>')
                z.writestr('EPUB/toc.ncx', '<ncx><navMap><navPoint><navLabel><text>Dedication</text></navLabel><content src="chapter.xhtml#gone"/></navPoint></navMap></ncx>')
                z.writestr('EPUB/nav.xhtml', '<html><body><nav epub:type="toc"><a href="chapter.xhtml#gone">Dedication</a></nav></body></html>')
                z.writestr('EPUB/chapter.xhtml', '<html><body><h1 id="valid">已经翻译</h1></body></html>')
            report = audit_translated_epub_output(path)
            self.assertEqual(report['navigation_residual_labels'], 1)
            self.assertEqual(report['navigation_broken_targets'], 1)
            qa = build_translation_qa_report(translation_stats={'artifact_audit': report}, output_path=path)
            self.assertFalse(qa['can_deliver'])
            self.assertIn('navigation_broken', qa['flags'])


if __name__ == '__main__': unittest.main()
