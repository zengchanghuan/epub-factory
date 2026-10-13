"""D89: book-independent terminology contracts and non-blocking QA signals."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault('OPENAI_API_KEY', 'dummy')

from bs4 import BeautifulSoup
from app.domain.translation_consistency_audit import audit_book_consistency
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_qa_service import build_translation_qa_report
from app.domain.translation_terms import term_spans
from app.engine.glossary_extractor import select_relevant_glossary, verify_and_fix


def chunk(source, target, identifier='sample'):
    return SimpleNamespace(original_html=source, translated_html=target, chunk_id=identifier, error=None)


class TerminologyContracts(unittest.TestCase):
    def test_blank_term_never_matches(self):
        self.assertEqual(term_spans(' \n\t', 'Aster'), [])

    def test_inline_and_entity_boundaries_cannot_create_partial_word_repairs(self):
        for target in ['<p>Aster<b>isk</b></p>', '<p><i>É</i>Aster</p>',
                       '<p>Aster&#105;sk</p>', '<p>&Eacute;Aster</p>']:
            with self.subTest(target=target):
                fixed, report = verify_and_fix('Aster arrived.', target, {'Aster': '星河'})
                self.assertEqual(fixed, target)
                self.assertEqual(report.fixed_count, 0)
                self.assertEqual(report.unfixable_examples, ['Aster→星河'])

    def test_split_urls_and_long_phrases_remain_intact(self):
        for target in ['<p>https://example.test/?q=x&amp;name=Aster</p>',
                       '<p>https://example.test/<i>Aster</i></p>',
                       '<p>doi:10.1234/<b>Aster</b></p>']:
            with self.subTest(target=target):
                fixed, report = verify_and_fix('Aster arrived.', target, {'Aster': '星河'})
                self.assertEqual(fixed, target)
                self.assertEqual(report.fixed_count, 0)
        target = '<p>New <b>Harbor</b> 与 New。新港</p>'
        fixed, report = verify_and_fix('New Harbor and New.', target, {'New Harbor': '新港', 'New': '纽'})
        self.assertEqual(fixed, '<p>New <b>Harbor</b> 与 纽。新港</p>')
        self.assertEqual(report.fixed_count, 1)

    def test_non_prose_cannot_supply_missing_canonical_translation(self):
        for evidence in ['<code>玛拉</code>', '<pre>玛拉</pre>',
                         '<span class="epub-original">玛拉</span>']:
            with self.subTest(evidence=evidence):
                source = '<p>Mara arrived.</p>'
                target = '<p>' + evidence + '<span>Mara来了。</span></p>'
                fixed, report = verify_and_fix(source, target, {'Mara': '玛拉'})
                self.assertEqual(fixed, target.replace('Mara来了', '玛拉来了'))
                self.assertEqual(report.fixed_count, 1)
                audit = audit_translation_chunk(original_html=source, translated_html=target, glossary={'Mara': '玛拉'})
                self.assertIn('glossary_terms_missing', audit.flags)
                book = audit_book_consistency(chapter_results={'one': [chunk(source, target)]}, glossary={'Mara': '玛拉'})
                self.assertEqual(book['consistency_violations'], 1)

    def test_block_boundaries_do_not_join_names_or_reference_identifiers(self):
        source = '<p>Aster</p><p>arrived.</p>'
        target = '<p>https://example.test/</p><p>Aster</p><p>来了。</p>'
        fixed, report = verify_and_fix(source, target, {'Aster': '星河'})
        self.assertEqual(fixed, target.replace('<p>Aster</p>', '<p>星河</p>'))
        self.assertEqual(report.fixed_count, 1)
        self.assertNotIn('glossary_terms_missing', audit_translation_chunk(
            original_html=source, translated_html='<p>星河来了。</p>', glossary={'Aster': '星河'}).flags)

    def test_longest_phrase_agrees_across_prompt_chunk_and_book_audits(self):
        glossary = {'New': '纽', 'New Harbor': '新港', 'Harbor': '港口'}
        source = '<p>New Harbor welcomes visitors.</p>'
        target = '<p>新港欢迎访客。</p>'
        self.assertEqual(select_relevant_glossary(glossary, 'New Harbor welcomes visitors.'), {'New Harbor': '新港'})
        self.assertNotIn('glossary_terms_missing', audit_translation_chunk(
            original_html=source, translated_html=target, glossary=glossary).flags)
        report = audit_book_consistency(chapter_results={'one': [chunk(source, target)]}, glossary=glossary)
        self.assertEqual((report['consistency_checks'], report['consistency_violations']), (1, 0))

    def test_repeated_term_does_not_consume_distinct_prompt_budget(self):
        selected = select_relevant_glossary({'Aster': '星', 'Lyra': '琴'}, 'Aster ' * 40 + 'Lyra', max_terms=2)
        self.assertEqual(selected, {'Aster': '星', 'Lyra': '琴'})
        self.assertEqual(select_relevant_glossary({'Aster': '星'}, 'Aster', max_terms=0), {})

    def test_retained_names_allow_case_spacing_and_adjacent_chinese_not_substrings(self):
        for name in ['Aster', 'Élodie', 'Mara Rowan']:
            with self.subTest(name=name):
                for target in ['这是'+name.lower()+'的故事。', name.replace(' ', '\n')+'来了。']:
                    report = audit_translation_chunk(original_html='<p>'+name+'</p>', translated_html='<p>'+target+'</p>', glossary={name: name})
                    self.assertNotIn('glossary_terms_missing', report.flags)
                report = audit_translation_chunk(original_html='<p>'+name+'</p>', translated_html='<p>'+name+'son来了。</p>', glossary={name: name})
                self.assertIn('glossary_terms_missing', report.flags)

    def test_translated_latin_target_requires_a_whole_name(self):
        report = audit_translation_chunk(original_html='<p>星河来了。</p>', translated_html='<p>Asterson来了。</p>', glossary={'星河': 'Aster'})
        self.assertIn('glossary_terms_missing', report.flags)

    def test_profile_cannot_override_confirmed_glossary(self):
        for profile_name in ['Mara', 'MARA']:
            with self.subTest(profile_name=profile_name):
                report = audit_book_consistency(chapter_results={'one': [chunk('Mara arrived.', '玛拉来了。')]},
                    glossary={'Mara': '玛拉'}, characters=[{'source_name': profile_name, 'translated_name': '马拉'}])
                self.assertEqual(report['consistency_violations'], 0)
                self.assertEqual(report['consistency_checks'], 1)

    def test_small_caps_do_not_hide_a_term_from_book_audit(self):
        report = audit_book_consistency(chapter_results={'one': [chunk('<p>M<small>ARA</small> arrived.</p>', '<p>她来了。</p>')]}, glossary={'Mara': '玛拉'})
        self.assertEqual(report['consistency_violations'], 1)

    def test_multiple_terms_in_one_chunk_do_not_inflate_chunk_count(self):
        report = audit_book_consistency(chapter_results={'one': [chunk('Mara visited New Harbor.', '她去了那里。')]}, glossary={'Mara': '玛拉', 'New Harbor': '新港'}, example_limit=0)
        self.assertEqual(report['consistency_violations'], 2)
        self.assertEqual(report['consistency_review_chunks'], 1)
        self.assertEqual(report['consistency_status'], 'review')
        self.assertEqual(report['consistency_examples'], [])

    def test_missing_glossary_is_not_claimed_as_verified_consistency(self):
        report = audit_book_consistency(chapter_results={'one': [chunk('Mara arrived.', '马拉来了。')]}, glossary={})
        self.assertEqual(report['consistency_status'], 'not_checked')
        self.assertEqual(report['consistency_checks'], 0)

    def test_repair_touches_visible_text_not_attributes_or_reference_identifiers(self):
        target = ('<p id="Aster"><a href="https://example.test/Aster" title="Aster">Aster</a>来了。'
                  '<code>Aster</code><svg><text>Aster</text></svg><!-- Aster -->'
                  ' https://example.test/Aster doi:10.1234/Aster</p>')
        fixed, report = verify_and_fix('<p>Aster arrived.</p>', target, {'Aster': '星河'})
        soup = BeautifulSoup(fixed, 'html.parser')
        self.assertEqual(soup.a.get_text(), '星河')
        self.assertEqual(soup.a['href'], 'https://example.test/Aster')
        self.assertEqual(soup.a['title'], 'Aster')
        self.assertEqual(soup.p['id'], 'Aster')
        self.assertEqual(soup.code.get_text(), 'Aster')
        self.assertEqual(soup.svg.get_text(), 'Aster')
        self.assertIn('<!-- Aster -->', fixed)
        self.assertIn('doi:10.1234/Aster', fixed)
        self.assertEqual(report.fixed_count, 1)

    def test_repair_preserves_original_markup_bytes_and_escapes_only_new_text(self):
        source = '<p>Aster arrived.</p>'
        target = ("<p title='Aster &amp; Co'>\r前文\u2028\nAster来了。\n"
                  '<svg viewBox="0 0 10 10"><linearGradient id="Aster"/></svg>'
                  '<math><mi>Aster</mi></math>&amp; tail</p>')
        fixed, report = verify_and_fix(source, target, {'Aster': '星<&河'})
        self.assertEqual(fixed, target.replace('Aster来了', '星&lt;&amp;河来了'))
        self.assertEqual(report.fixed_count, 1)

    def test_source_attributes_cannot_trigger_a_repair(self):
        source = '<p id="Mara">A story.</p>'
        target = '<p>Mara的故事。</p>'
        self.assertEqual(verify_and_fix(source, target, {'Mara': '玛拉'})[0], target)

    def test_bilingual_original_is_never_rewritten(self):
        target = '<p><span class="epub-original">Mara</span><br/><span class="epub-translated">Mara来了。</span></p>'
        fixed, result = verify_and_fix('<p>Mara arrived.</p>', target, {'Mara': '玛拉'})
        soup = BeautifulSoup(fixed, 'html.parser')
        self.assertEqual(soup.select_one('.epub-original').get_text(), 'Mara')
        self.assertEqual(soup.select_one('.epub-translated').get_text(), '玛拉来了。')
        self.assertEqual(result.fixed_count, 1)

    def test_ambiguous_cross_mappings_are_not_guessed_and_simple_repair_is_idempotent(self):
        fixed, report = verify_and_fix('Aster and Lyra.', '<p>Aster与Lyra。</p>', {'Aster': 'Lyra', 'Lyra': '琴'})
        self.assertEqual(fixed, '<p>Aster与Lyra。</p>')
        self.assertEqual(report.fixed_count, 0)
        self.assertTrue(report.unfixable_examples)
        for other in ['New Harbor', 'NEW HARBOR', 'New  Harbor']:
            with self.subTest(other=other):
                target = '<p>Aster与New Harbor。</p>'
                fixed, report = verify_and_fix('Aster and New Harbor.', target, {'Aster': 'New Harbor', other: '新港'})
                self.assertEqual(fixed, target)
                self.assertEqual(report.fixed_count, 0)
        simple, _ = verify_and_fix('Aster arrived.', '<p>Aster来了。</p>', {'Aster': '星河'})
        again, second = verify_and_fix('Aster arrived.', simple, {'Aster': '星河'})
        self.assertEqual(again, simple)
        self.assertEqual(second.fixed_count, 0)

    def test_repair_does_not_change_inline_split_terms_or_protected_source_phrases(self):
        target = '<p>New Harbor 与 New。新港</p>'
        fixed, report = verify_and_fix('New Harbor and New.', target, {'New Harbor': '新港', 'New': '纽'})
        self.assertEqual(fixed, '<p>New Harbor 与 纽。新港</p>')
        self.assertEqual(report.fixed_count, 1)
        split = '<p>Ma<b>ra</b>来了。</p>'
        self.assertEqual(verify_and_fix('Mara arrived.', split, {'Mara': '玛拉'})[0], split)

    def test_unknown_transliteration_is_reported_not_guessed(self):
        original = '<p>Mara Rowan arrived.</p>'
        translated = '<p>马拉·罗温来了。</p>'
        fixed, report = verify_and_fix(original, translated, {'Mara Rowan': '玛拉·罗恩'})
        self.assertEqual(fixed, translated)
        self.assertEqual(report.fixed_count, 0)
        self.assertEqual(report.unfixable_examples, ['Mara Rowan→玛拉·罗恩'])

    def test_citation_issue_number_is_not_negation_but_title_negation_is(self):
        for identifier in ['3', 'a1234']:
            with self.subTest(identifier=identifier):
                source = '<p>M. Rowan, “Attention in Play,” Research Review 12, no. '+identifier+' (2014).</p>'
                target = '<p>罗恩，《游戏中的注意力》，研究评论 12，第'+identifier+'期（2014）。</p>'
                self.assertNotIn('negation', audit_translation_chunk(original_html=source, translated_html=target).critical_markers_missing)
                bad = source.replace('Attention in Play', 'No Improvement in Play')
                self.assertIn('negation', audit_translation_chunk(original_html=bad, translated_html=target).critical_markers_missing)
        self.assertIn('negation', audit_translation_chunk(original_html='<p>No. 3 participants improved.</p>', translated_html='<p>三位参与者改善了。</p>').critical_markers_missing)

    def test_book_warning_surfaces_in_qa_without_blocking_or_retry(self):
        stats = audit_book_consistency(chapter_results={'one': [chunk('Mara arrived.', '马拉来了。')]}, glossary={'Mara': '玛拉'})
        stats.update(artifact_audit={'status': 'passed'}, failed_chunks=0, audit_warn_chunks=1)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'synthetic.epub';output.write_bytes(b'fixture')
            qa = build_translation_qa_report(translation_stats=stats, output_path=output)
            self.assertEqual(qa['status'], 'warning')
            self.assertIn('术语', qa['summary'])
            self.assertTrue(qa['can_deliver'])
            self.assertFalse(qa['retryable'])
            self.assertEqual(qa['review_chunks'], 1)  # no summing overlapping signals
            self.assertEqual(qa['consistency_review_chunks'], 1)
            stats['failed_chunks'] = 1
            failed = build_translation_qa_report(translation_stats=stats, output_path=output)
            self.assertEqual(failed['status'], 'failed')
            self.assertFalse(failed['can_deliver'])


if __name__ == '__main__':
    unittest.main()
