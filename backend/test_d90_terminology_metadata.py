"""D90: confirmed alias propagation and legacy resume compatibility, offline."""
import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.main import _confirmed_translation_preflight
from app.domain.fast_translation_runner import _build_glossary_catalog
from app.domain.translation_checkpoints import book_resume_key
from app.domain.translation_consistency_audit import audit_book_consistency
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_terminology_metadata import (
    apply_terminology_review, generation_preflight, normalize_terminology_review, preflight_terminology_review,
)
from app.domain.translation_terminology_policy import terminology_evidence
from app.engine.glossary_extractor import verify_and_fix


SOURCE, TARGET, ALIAS = 'Mara Rowan', '玛拉·罗恩', '马拉·罗温'


def review_row(**changes):
    return dict(dict(source=SOURCE, translation=TARGET, entity_id='mara rowan', status='confirmed',
                     target_aliases=[dict(translation=ALIAS, status='confirmed')]), **changes)


def snapshot():
    return dict(version=4, confirmed=True, confirmed_at='2026-01-01T00:00:00+00:00',
                glossary={SOURCE: TARGET}, glossary_catalog=[dict(source=SOURCE, translation=TARGET,
                    origin='user', status='confirmed')], characters=[], profile={'characters': []},
                resolved_strategy='neutral_faithful')


class TerminologyMetadataTests(unittest.TestCase):
    def confirm(self, payload, existing=None):
        return _confirmed_translation_preflight(existing=existing or snapshot(), payload=payload)[0]

    def catalog(self, confirmed):
        glossary = confirmed['glossary']
        return _build_glossary_catalog(glossary_result=SimpleNamespace(candidates=[], user_glossary=glossary),
            merged_glossary=glossary, character_glossary={},
            terminology_review=confirmed.get('terminology_review'), characters=confirmed.get('characters'))

    def test_confirmation_json_roundtrip_reaches_verifier_chunk_and_book(self):
        before = snapshot()
        confirmed = self.confirm({'glossary_catalog': [review_row()]}, before)
        self.assertEqual(before, snapshot())
        confirmed = json.loads(json.dumps(confirmed))
        catalog = self.catalog(confirmed)
        self.assertEqual(catalog[0]['entity_id'], 'mara rowan')
        self.assertEqual(catalog[0]['target_aliases'][0]['translation'], ALIAS)
        source, target = SOURCE + ' arrived.', ALIAS + '来了。'
        fixed, result = verify_and_fix(source, target, confirmed['glossary'], glossary_catalog=catalog)
        self.assertEqual(fixed, target)
        self.assertEqual(result.fixed_count, 0)
        self.assertFalse(audit_translation_chunk(original_html=source, translated_html=target,
            glossary=confirmed['glossary'], glossary_catalog=catalog).terminology_findings)
        report = audit_book_consistency(chapter_results={'one': [SimpleNamespace(original_html=source,
            translated_html=target, error=None)]}, glossary=confirmed['glossary'], glossary_catalog=catalog)
        self.assertEqual(report['consistency_status'], 'passed')

    def test_character_edits_store_qa_metadata_outside_generation_profile(self):
        character = dict(source_name=SOURCE, translated_name=TARGET, aliases=['Rowan'], status='confirmed')
        plain = self.confirm({'characters': [character]})
        enriched = self.confirm({'characters': [dict(character, entity_id='MARA ROWAN',
            target_aliases=review_row()['target_aliases'])]})
        self.assertEqual(plain['profile'], enriched['profile'])
        self.assertEqual(plain['characters'], enriched['characters'])
        self.assertEqual(enriched['terminology_review'][0]['entity_id'], 'mara rowan')

    def test_api_rejects_stale_generated_wrong_entity_and_duplicate_evidence(self):
        rows = [review_row(translation='旧译名'), review_row(status='generated'),
                review_row(entity_id='unrelated'),
                review_row(target_aliases=[dict(translation=ALIAS, status='generated')]),
                review_row(target_aliases=[dict(translation=ALIAS, status='confirmed', entity_id='someone else')])]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(HTTPException) as failure:
                self.confirm({'terminology_review': [row]})
            self.assertEqual(failure.exception.status_code, 400)
        for rows in [[review_row(), review_row(source='mara rowan')], 'invalid', [None],
                     [review_row(target_aliases=['bare alias'])], [review_row()] * 241]:
            with self.subTest(rows=rows), self.assertRaises(HTTPException):
                self.confirm({'terminology_review': rows})

    def test_unchanged_metadata_survives_omission_but_can_be_revoked(self):
        confirmed = self.confirm({'terminology_review': [review_row()]})
        inherited = self.confirm({}, confirmed)
        self.assertEqual(inherited['terminology_review'], confirmed['terminology_review'])
        revoked = self.confirm({'terminology_review': []}, confirmed)
        self.assertEqual(revoked['terminology_review'], [])
        self.assertEqual(terminology_evidence(SOURCE, ALIAS, revoked['glossary'], self.catalog(revoked))[0]
                         ['category'], 'glossary_terms_missing')

    def test_mapping_change_discards_old_metadata_and_requires_explicit_reconfirmation(self):
        existing = self.confirm({'terminology_review': [review_row()]})
        changed = self.confirm({'glossary': {SOURCE: '新译名'}}, existing)
        self.assertEqual(changed['terminology_review'], [])
        catalog = self.catalog(changed)
        self.assertNotIn('target_aliases', catalog[0])
        # Revalidation also protects a stale snapshot supplied straight to the worker.
        self.assertEqual(apply_terminology_review(catalog, existing['terminology_review']), catalog)

    def test_legacy_catalog_metadata_preserved_only_for_unchanged_mapping(self):
        old = snapshot()
        old['glossary_catalog'] = [dict(review_row(), origin='user')]
        rebuilt = _build_glossary_catalog(glossary_result=SimpleNamespace(candidates=[], user_glossary=old['glossary']),
            merged_glossary=old['glossary'], character_glossary={},
            terminology_review=preflight_terminology_review(old))
        self.assertEqual(terminology_evidence(SOURCE, ALIAS, old['glossary'], rebuilt)[0]['category'], 'matched')
        self.assertTrue(self.confirm({}, old)['terminology_review'])
        changed = self.confirm({'glossary': {SOURCE: '新译名'}}, old)
        self.assertEqual(changed['terminology_review'], [])
        self.assertNotIn('target_aliases', changed['glossary_catalog'][0])

    def test_revoking_legacy_aliases_does_not_reactivate_them_on_next_confirmation(self):
        old = snapshot()
        old['glossary_catalog'] = [dict(review_row(), origin='user')]
        revoked = self.confirm({'terminology_review': []}, old)
        self.assertEqual(preflight_terminology_review(revoked), [])
        self.assertEqual(self.confirm({}, revoked)['terminology_review'], [])
        self.assertNotIn('target_aliases', self.catalog(revoked)[0])

    def test_shared_surname_requires_known_full_identity(self):
        characters = [dict(source_name=name, translated_name=target, aliases=['Rowan']) for name, target in
                      [(SOURCE, TARGET), ('Lena Rowan', '莉娜·罗恩')]]
        glossary = {'Rowan': '罗恩'}
        row = dict(source='Rowan', translation='罗恩', status='confirmed', entity_id='mara rowan')
        for identity in ['Rowan', 'unknown', None]:
            with self.subTest(identity=identity):
                self.assertEqual(normalize_terminology_review([dict(row, entity_id=identity)], glossary, characters), [])
        catalog = apply_terminology_review([dict(row, origin='auto', status='generated')], [row], characters)
        report = audit_book_consistency(chapter_results={'one': [SimpleNamespace(original_html='Rowan arrived.',
            translated_html='罗恩来了。', error=None)]}, glossary=glossary, characters=characters, glossary_catalog=catalog)
        self.assertEqual(report['consistency_status'], 'passed')

    def test_accepting_alias_does_not_mask_second_missing_mention_or_annotation(self):
        confirmed = self.confirm({'terminology_review': [review_row()]})
        catalog = self.catalog(confirmed)
        for target in [ALIAS + '来了。另一人离开了。', TARGET + '（' + ALIAS + '）来了。另一人离开了。']:
            with self.subTest(target=target):
                evidence = terminology_evidence(SOURCE + ' arrived. ' + SOURCE + ' left.', target,
                                                confirmed['glossary'], catalog)[0]
                self.assertEqual(evidence['category'], 'glossary_occurrence_unresolved')
                self.assertEqual(evidence['target_occurrences'], 1)

    def test_legacy_resume_key_is_identical_and_qa_edits_do_not_change_it(self):
        for old in [None, {}, snapshot(), dict(snapshot(), pdf={'pages': []})]:
            with self.subTest(preflight=old):
                job = SimpleNamespace(input_path='/nonexistent/offline.epub',
                    translation_stats={'translation_preflight': old})
                # The old implementation hashed preflight unchanged.
                with patch('app.domain.translation_checkpoints.generation_preflight', side_effect=lambda value: value):
                    legacy = book_resume_key(job, {'chapters': []})
                self.assertEqual(book_resume_key(job, {'chapters': []}), legacy)
                if isinstance(old, dict):
                    enriched = copy.deepcopy(old)
                    enriched['terminology_review'] = [review_row()]
                    job.translation_stats['translation_preflight'] = enriched
                    self.assertEqual(book_resume_key(job, {'chapters': []}), legacy)
                    enriched['terminology_review'] = []
                    self.assertEqual(book_resume_key(job, {'chapters': []}), legacy)
                    self.assertEqual(generation_preflight(enriched), old)

    def test_resume_still_invalidates_generation_changes(self):
        old = snapshot()
        job = SimpleNamespace(input_path='/nonexistent/offline.epub', translation_stats={'translation_preflight': old})
        key = book_resume_key(job, {'chapters': []})
        for field, value in [('glossary', {SOURCE: '新译名'}), ('resolved_strategy', 'literary_narrative'),
                             ('characters', [dict(source_name=SOURCE, translated_name='新译名')]),
                             ('chapter_strategy_overrides', {'one': 'academic_rigorous'}), ('version', 99)]:
            with self.subTest(field=field):
                job.translation_stats['translation_preflight'] = dict(old, **{field: value})
                self.assertNotEqual(book_resume_key(job, {'chapters': []}), key)

    def test_full_worker_receives_confirmed_metadata_and_persists_catalog(self):
        from test_d14_fast_translation_runner import _make_epub, run_fast_translation_job
        from app.domain import fast_translation_runner as runner
        from app.engine.cleaners.semantics_translator import SemanticsTranslator
        from app.models import Job, OutputMode
        from app.storage import job_store
        marker = uuid.uuid4().hex[:10]
        confirmed = self.confirm({'glossary': {'Smith': '史密斯'}, 'terminology_review': [
            dict(source='Smith', translation='史密斯', status='confirmed', target_aliases=[
                dict(translation='斯密斯', status='confirmed')])]})
        async def fake_call(self, payload, **kwargs):
            return ({item['id']: ('注释图解版双螺旋' if 'Double Helix' in item['html'] else
                '斯密斯与华生和富兰克林前往伦敦。' + marker if 'Smith' in item['html'] else '第一章 1')
                for item in payload}, {'model': 'offline', 'base_url': 'mock://offline'})
        with tempfile.TemporaryDirectory() as tmp, \
             patch('app.engine.glossary_service.translate_glossary', new=AsyncMock(return_value={})), \
             patch.object(SemanticsTranslator, '_call_llm_json_batch', new=fake_call), \
             patch.object(runner, 'audit_translation_chunk', wraps=audit_translation_chunk) as chunks, \
             patch.object(runner, 'audit_book_consistency', wraps=audit_book_consistency) as book:
            source = Path(tmp) / 'input.epub'
            output = Path(tmp) / 'output.epub'
            _make_epub(source, marker)
            job = Job(id=uuid.uuid4().hex, trace_id='offline', source_filename='input.epub', input_path=str(source),
                output_mode=OutputMode.simplified, enable_translation=True, glossary=confirmed['glossary'],
                translation_stats={'translation_preflight': confirmed})
            job_store.add(job)
            result = run_fast_translation_job(job=job, input_path=source, output_path=output,
                progress_callback=lambda _: None, stage_callback=lambda *_: None)
            self.assertTrue(output.is_file())
            catalog = result.translation_stats['glossary_catalog']
            row = next(row for row in catalog if row['source'] == 'Smith')
            self.assertEqual(row['target_aliases'][0]['translation'], '斯密斯')
            self.assertEqual(book.call_args.kwargs['glossary_catalog'], catalog)
            self.assertTrue(chunks.called)
            for call in chunks.call_args_list:
                self.assertEqual(call.kwargs['glossary_catalog'], catalog)


if __name__ == '__main__':
    unittest.main()
