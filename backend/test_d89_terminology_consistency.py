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
from app.engine.glossary_extractor import extract_candidates, select_relevant_glossary, verify_and_fix


def chunk(source, target, identifier='sample'):
    return SimpleNamespace(original_html=source, translated_html=target, chunk_id=identifier, error=None)


class TerminologyContracts(unittest.TestCase):
    def test_parenthetical_original_names_are_never_rewritten(self):
        row = dict(source='Mara Rowan', translation='玛拉·罗恩', origin='auto', status='generated')
        for origin, status in [('auto', 'generated'), ('user', 'confirmed')]:
            for annotation in ['（Mara Rowan，心理学家）', '(Mara Rowan, psychologist)',
                               '（<i>Mara</i> Rowan，心理学家）',
                               '（Mara&#32;Rowan）', '(notes （Mara Rowan）)']:
                with self.subTest(origin=origin, annotation=annotation):
                    target = '<p>马拉·罗温' + annotation + '发言。</p>'
                    fixed, result = verify_and_fix('Mara Rowan spoke.', target,
                        {'Mara Rowan': '玛拉·罗恩'}, glossary_catalog=[dict(row, origin=origin, status=status)])
                    self.assertEqual(fixed, target)
                    self.assertEqual(result.fixed_count, 0)
                    self.assertTrue(result.unfixable_examples)

    def test_parentheses_do_not_disable_plain_prose_repair(self):
        target = '<p>Mara Rowan发言（Mara Rowan，心理学家）。</p>'
        fixed, report = verify_and_fix('Mara Rowan spoke.', target, {'Mara Rowan': '玛拉·罗恩'})
        self.assertEqual(fixed, '<p>玛拉·罗恩发言（Mara Rowan，心理学家）。</p>')
        self.assertEqual(report.fixed_count, 1)
        self.assertEqual(verify_and_fix('Mara Rowan spoke.', fixed, {'Mara Rowan': '玛拉·罗恩'})[0], fixed)

    def test_repeated_source_with_one_target_is_uncertain_not_verified(self):
        for target in ['玛拉·罗恩来了。马拉·罗温离开了。', '玛拉·罗恩来了。她离开了。']:
            audit = audit_translation_chunk(original_html='Mara Rowan arrived. Mara Rowan left.',
                translated_html=target, glossary={'Mara Rowan': '玛拉·罗恩'})
            self.assertIn('glossary_occurrence_unresolved', audit.flags)
            self.assertEqual(audit.risk_level, 'warn')
            finding = audit.terminology_findings[0]
            self.assertEqual(finding['review_reasons'], ['source_occurrence_gap'])
            self.assertEqual((finding['source_occurrences'], finding['target_occurrences']), (2, 1))
            self.assertEqual(len(finding['source_spans']), 2)
        audit = audit_translation_chunk(original_html='Mara Rowan arrived. Mara Rowan left.',
            translated_html='玛拉·罗恩来了。玛拉·罗恩离开了。', glossary={'Mara Rowan': '玛拉·罗恩'})
        self.assertFalse(audit.terminology_findings)

    def test_distinct_entities_cannot_share_overlapping_target_evidence(self):
        for glossary, target, count in [({'Mara Rowan': '玛拉·罗恩', 'Lena Rowan': '罗恩'}, '玛拉·罗恩来了。', 1),
                                 ({'Mara Rowan': '罗恩', 'Lena Rowan': '罗恩'}, '罗恩来了。', 2)]:
            audit = audit_translation_chunk(original_html='Mara Rowan and Lena Rowan arrived.',
                translated_html=target, glossary=glossary)
            self.assertEqual(len(audit.terminology_findings), count)
            self.assertTrue(all('shared_target_span' in r['review_reasons'] for r in audit.terminology_findings))
            self.assertTrue(all(r['category'] == 'glossary_occurrence_unresolved' for r in audit.terminology_findings))
        audit = audit_translation_chunk(original_html='Mara Rowan and Lena Rowan arrived.',
            translated_html='玛拉·罗恩和莉娜·罗恩来了。',
            glossary={'Mara Rowan': '玛拉·罗恩', 'Lena Rowan': '莉娜·罗恩'})
        self.assertFalse(audit.terminology_findings)
        audit = audit_translation_chunk(original_html='Mara Rowan and Lena Rowan arrived.',
            translated_html='玛拉·罗恩和罗恩来了。',
            glossary={'Mara Rowan': '玛拉·罗恩', 'Lena Rowan': '罗恩'})
        self.assertFalse(audit.terminology_findings)

    def test_nested_aliases_and_source_phrases_do_not_inflate_evidence(self):
        from app.domain.translation_terminology_policy import terminology_evidence
        rows = [dict(source='Mara Rowan', translation='玛拉·罗恩', origin='user', status='confirmed',
                     target_aliases=[{'translation': '罗恩', 'status': 'confirmed'}])]
        evidence = terminology_evidence('Mara Rowan arrived. Mara Rowan left.', '玛拉·罗恩来了。',
                                        {'Mara Rowan': '玛拉·罗恩'}, rows)
        self.assertEqual(evidence[0]['target_occurrences'], 1)
        self.assertEqual(evidence[0]['category'], 'glossary_occurrence_unresolved')
        evidence = terminology_evidence('New Harbor and Harbor.', '新港和港口。',
                                        {'New Harbor': '新港', 'Harbor': '港口'})
        self.assertEqual([r['source_occurrences'] for r in evidence], [1, 1])
        self.assertTrue(all(r['category'] == 'matched' for r in evidence))

    def test_explicit_same_entity_and_confirmed_aliases_share_evidence(self):
        glossary = {'Mara Rowan': '玛拉·罗恩', 'Rowan': '罗恩'}
        catalog = [dict(source=s, translation=t, origin='user', status='confirmed', entity_id='person-1')
                   for s, t in glossary.items()]
        audit = audit_translation_chunk(original_html='Mara Rowan arrived. Rowan left.',
            translated_html='玛拉·罗恩来了。罗恩离开了。', glossary=glossary, glossary_catalog=catalog)
        self.assertFalse(audit.terminology_findings)
        catalog[0]['target_aliases'] = [{'translation': '马拉·罗温', 'status': 'confirmed'}]
        audit = audit_translation_chunk(original_html='Mara Rowan arrived. Mara Rowan left.',
            translated_html='玛拉·罗恩来了。马拉·罗温离开了。', glossary=glossary, glossary_catalog=catalog)
        self.assertFalse(audit.terminology_findings)

    def test_parenthetical_chinese_variants_are_reviewed_without_guessing(self):
        for target in ['马拉·罗温（玛拉·罗恩，心理学家）发言。',
                       '玛拉·罗恩（马拉·罗温，心理学家）发言。']:
            audit = audit_translation_chunk(original_html='Mara Rowan spoke.', translated_html=target,
                glossary={'Mara Rowan': '玛拉·罗恩'})
            self.assertIn('parenthetical_name_variants', audit.terminology_findings[0]['review_reasons'])
            self.assertEqual(verify_and_fix('Mara Rowan spoke.', target, {'Mara Rowan': '玛拉·罗恩'})[0], target)
        for target in ['玛拉·罗恩（心理学家）发言。', '玛拉·罗恩（Mara Rowan）发言。',
                       '玛拉·罗恩（玛拉·罗恩，心理学家）发言。']:
            self.assertFalse(audit_translation_chunk(original_html='Mara Rowan spoke.', translated_html=target,
                glossary={'Mara Rowan': '玛拉·罗恩'}).terminology_findings)

    def test_occurrence_uncertainty_is_nonblocking_and_visible_in_book_qa(self):
        report = audit_book_consistency(chapter_results={'one': [chunk(
            'Mara Rowan arrived. Mara Rowan left.', '玛拉·罗恩来了。马拉·罗温离开了。')]},
            glossary={'Mara Rowan': '玛拉·罗恩'})
        self.assertEqual(report['consistency_categories'], {'glossary_occurrence_unresolved': 1})
        self.assertEqual(report['consistency_mixed_entity_count'], 1)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'sample.epub'; output.write_bytes(b'fixture')
            report.update(artifact_audit={'status': 'passed'}, failed_chunks=0)
            qa = build_translation_qa_report(translation_stats=report, output_path=output)
            self.assertIn('出现位置待核对', qa['summary'])
            self.assertTrue(qa['can_deliver'])
            self.assertFalse(qa['retryable'])

    def test_occurrence_warning_reuses_checkpoint_without_another_model_request(self):
        import asyncio
        from unittest.mock import patch
        import test_d29_translation_performance as fixture
        fixture.PerformanceTests.setUp(self)
        source = 'Mara Rowan arrived. Mara Rowan left.'
        target = '玛拉·罗恩来了。马拉·罗温（Mara Rowan）离开了。'
        manifest = fixture.manifest_for(source)
        glossary = {'Mara Rowan': '玛拉·罗恩'}
        self.job.glossary = glossary
        calls = []

        async def request(translator, payload):
            calls.append(payload)
            return fixture.reply({item['id']: target for item in payload})

        def run():
            return asyncio.run(fixture._translate_manifest_async(
                job=self.job, manifest=manifest, glossary=glossary,
                content_by_file={'c0.xhtml': ('<html><body><p>' + source + '</p></body></html>').encode()},
                progress_callback=lambda _: None))

        with patch.object(fixture.SemanticsTranslator, '_call_llm_json_batch', new=request):
            first, _ = run()
            second, _ = run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(first['failed_chunks'], 0)
        self.assertEqual(first['audit_flags_count']['glossary_occurrence_unresolved'], 1)
        self.assertEqual(second['checkpoint_resumed_chunks'], 1)
        self.assertEqual(second['checkpoint_rejected_chunks'], 0)
        self.assertEqual(second['glossary_fixed_count'], 0)

    def test_name_particles_are_not_split_and_prose_is_not_joined(self):
        names = ['Nora van de Veen', 'Luca della Torre', 'Émile de La Roche', 'Anna von Berg']
        for name in names:
            with self.subTest(name=name):
                found, _ = extract_candidates([f'We discussed {name}.', f'Dr. {name} arrived.'], min_count=1)
                terms = {c.term for c in found}
                self.assertIn(name, terms)
                self.assertNotIn(name.split()[-1], terms)
        found, _ = extract_candidates(['We saw Alice meet Bob. Alice saw Bob.'], min_count=1)
        self.assertFalse(any('meet' in c.term or 'saw' in c.term for c in found))

    def test_unrelated_single_cjk_character_cannot_satisfy_a_name(self):
        for catalog in [None, [{'source': 'Veen', 'translation': '文', 'origin': 'auto', 'status': 'generated'}]]:
            for target in ['诺拉·范德芬的文献。', '诺拉·范德芬。']:
                report = audit_translation_chunk(original_html='Nora van de Veen wrote a paper.',
                    translated_html=target, glossary={'Veen': '文'}, glossary_catalog=catalog)
                self.assertEqual(report.latin_terms_missing, ['Veen'])
                self.assertIn('glossary_mapping_ambiguous' if catalog else 'glossary_terms_missing', report.flags)
        self.assertNotIn('glossary_terms_missing', audit_translation_chunk(
            original_html='The name is Veen.', translated_html='名字是“文”。', glossary={'Veen': '文'}).flags)

    def test_origin_and_status_distinguish_suggestion_from_contract(self):
        for origin, status, flag in [('auto', 'generated', 'glossary_candidate_difference'),
                ('book_profiler', 'generated', 'glossary_candidate_difference'),
                ('user', 'confirmed', 'glossary_terms_missing'),
                ('global', 'curated', 'glossary_terms_missing'),
                ('unknown', 'generated', 'glossary_terms_missing')]:
            with self.subTest(origin=origin):
                row = dict(source='Mara Rowan', translation='玛拉·罗恩', origin=origin, status=status)
                audit = audit_translation_chunk(original_html='Mara Rowan arrived.', translated_html='马拉·罗温来了。',
                    glossary={'Mara Rowan': '玛拉·罗恩'}, glossary_catalog=[row])
                self.assertEqual(audit.latin_terms_missing, ['Mara Rowan'])
                self.assertIn(flag, audit.flags)
                self.assertEqual(audit.risk_level, 'warn')
                self.assertEqual(audit.terminology_findings[0]['origin'], origin)

    def test_stale_or_conflicting_catalog_cannot_downgrade_user_mapping(self):
        auto = dict(source='Mara', translation='马拉', origin='auto', status='generated')
        for catalog in [[auto], [auto, dict(auto, origin='user', status='confirmed')]]:
            expected = '玛拉' if len(catalog) == 1 else '马拉'
            audit = audit_translation_chunk(original_html='Mara arrived.', translated_html='她来了。',
                glossary={'Mara': expected}, glossary_catalog=catalog)
            self.assertIn('glossary_terms_missing', audit.flags)

    def test_only_explicitly_confirmed_target_aliases_are_accepted(self):
        row = dict(source='Mara Rowan', translation='玛拉·罗恩', origin='auto', status='generated')
        for status, accepted in [('generated', False), ('confirmed', True)]:
            catalog = [dict(row, target_aliases=[{'translation': '马拉·罗温', 'status': status}])]
            audit = audit_translation_chunk(original_html='Mara Rowan arrived.', translated_html='马拉·罗温来了。',
                glossary={'Mara Rowan': '玛拉·罗恩'}, glossary_catalog=catalog)
            self.assertEqual(not audit.latin_terms_missing, accepted)
        # A category word is never inferred as a brand alias.
        audit = audit_translation_chunk(original_html='Drink FizzBrand.', translated_html='喝汽水。',
            glossary={'FizzBrand': '菲兹牌'}, glossary_catalog=[dict(row, source='FizzBrand', translation='菲兹牌')])
        self.assertIn('glossary_candidate_difference', audit.flags)

    def test_book_mixed_evidence_is_separate_from_candidate_difference(self):
        row = dict(source='Mara Rowan', translation='玛拉·罗恩', origin='auto', status='generated', entity_id='person-1')
        chapters = {'one': [chunk('Mara Rowan arrived.', '马拉·罗温来了。')],
                    'two': [chunk('Mara Rowan left.', '玛拉·罗恩走了。')]}
        report = audit_book_consistency(chapter_results=chapters, glossary={'Mara Rowan': '玛拉·罗恩'}, glossary_catalog=[row])
        self.assertEqual(report['consistency_categories'], {'glossary_candidate_difference': 1})
        self.assertEqual(report['consistency_mixed_entity_count'], 1)
        self.assertEqual(report['consistency_mixed_entities'][0]['source_terms'], ['Mara Rowan'])
        self.assertFalse(report['delivery_gate'])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'sample.epub'; output.write_bytes(b'fixture')
            report.update(artifact_audit={'status': 'passed'}, failed_chunks=0)
            qa = build_translation_qa_report(translation_stats=report, output_path=output)
            self.assertIn('自动候选译名差异', qa['summary'])
            self.assertNotIn('约定译名', qa['summary'])
            self.assertTrue(qa['can_deliver'])
            self.assertFalse(qa['retryable'])

    def test_same_surname_does_not_merge_different_full_names(self):
        glossary = {'Mara Rowan': '玛拉·罗恩', 'Lena Rowan': '莉娜·罗恩'}
        rows = [dict(source=s, translation=t, origin='auto', status='generated') for s, t in glossary.items()]
        report = audit_book_consistency(chapter_results={'one': [
            chunk('Mara Rowan arrived.', '玛拉·罗恩来了。'),
            chunk('Lena Rowan left.', '莱娜·罗温走了。')]}, glossary=glossary, glossary_catalog=rows)
        self.assertEqual(report['consistency_mixed_entity_count'], 0)
        self.assertEqual(report['consistency_violations'], 1)

    def test_short_ordinary_word_and_pronoun_remain_valid(self):
        for source, target, glossary in [('I arrived.', '我来了。', {'I': '我'}),
                ('The quantity increased.', '数量增加了。', {'quantity': '量'})]:
            self.assertFalse(audit_translation_chunk(original_html=source, translated_html=target,
                glossary=glossary).latin_terms_missing)

    def test_profile_aliases_keep_provenance_and_shared_surnames_need_review(self):
        characters = [{'source_name': 'Mara Rowan', 'translated_name': '玛拉·罗恩', 'aliases': ['Rowan']},
                      {'source_name': 'Lena Rowan', 'translated_name': '莉娜·罗恩', 'aliases': ['Rowan']}]
        report = audit_book_consistency(chapter_results={'one': [
            chunk('Mara Rowan arrived.', '马拉·罗温来了。'), chunk('Rowan left.', '罗恩走了。')]},
            glossary={}, characters=characters)
        self.assertEqual(report['consistency_categories'],
                         {'glossary_candidate_difference': 1, 'glossary_mapping_ambiguous': 1})
        self.assertEqual(report['consistency_ambiguous_source_aliases'], ['Rowan'])
        self.assertEqual(report['consistency_mixed_entity_count'], 0)
        self.assertEqual(report['consistency_checks'], 2)

    def test_runner_passes_catalog_to_chunk_and_book_audits(self):
        from unittest.mock import patch
        from test_d14_fast_translation_runner import test_fast_translation_runner_glossary_audit
        with patch('app.domain.fast_translation_runner.audit_translation_chunk', wraps=audit_translation_chunk) as chunks, \
             patch('app.domain.fast_translation_runner.audit_book_consistency', wraps=audit_book_consistency) as book:
            test_fast_translation_runner_glossary_audit()
        self.assertTrue(chunks.called)
        self.assertTrue(book.called)
        catalog = book.call_args.kwargs['glossary_catalog']
        self.assertTrue(any(row['origin'] == 'user' and row['status'] == 'confirmed' for row in catalog))
        for call in chunks.call_args_list:
            self.assertEqual(call.kwargs['glossary_catalog'], catalog)

    def test_generated_lowercase_name_key_cannot_bypass_short_mapping_guard(self):
        report = audit_translation_chunk(original_html='Nora van de Veen wrote a paper.',
            translated_html='诺拉·范德芬的文献。', glossary={'veen': '文'}, glossary_catalog=[
                dict(source='veen', translation='文', origin='auto', status='generated')])
        self.assertIn('glossary_mapping_ambiguous', report.flags)
        self.assertEqual(report.latin_terms_missing, ['veen'])

    def test_short_ambiguous_mapping_is_reviewed_without_guessing_a_repair(self):
        target = '<p>Veen的文献。</p>'
        fixed, report = verify_and_fix('Veen wrote a paper.', target, {'Veen': '文'}, glossary_catalog=[
            dict(source='Veen', translation='文', origin='auto', status='generated')])
        self.assertEqual(fixed, target)
        self.assertEqual(report.fixed_count, 0)
        self.assertEqual(report.unfixable_examples, ['Veen→文'])

    def test_confirmed_alias_of_another_entity_does_not_satisfy_mapping(self):
        row = dict(source='Mara Rowan', translation='玛拉·罗恩', origin='user', status='confirmed',
                   entity_id='person-1', target_aliases=[dict(translation='莉娜·罗恩', status='confirmed', entity_id='person-2')])
        report = audit_translation_chunk(original_html='Mara Rowan arrived.', translated_html='莉娜·罗恩来了。',
            glossary={'Mara Rowan': '玛拉·罗恩'}, glossary_catalog=[row])
        self.assertIn('glossary_terms_missing', report.flags)

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
