import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
spec = importlib.util.spec_from_file_location('book_corpus', ROOT / 'scripts/regression-book-corpus.py')
corpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(corpus)


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fixture.epub'
        with zipfile.ZipFile(self.path, 'w') as archive:
            archive.writestr('chapter.xhtml', '<html><body><p id="n">PRIVATE SENTENCE</p><p id="n">x</p>'
                '<a href="#missing">note</a><a href="https://invalid.example/">external</a>'
                '<img src="figure.png"/></body></html>')
            archive.writestr('figure.png', b'offline-asset')

    def test_no_external_fetch_and_counts_broken_note_and_duplicate_ids(self):
        result = corpus.epub_structure(self.path)
        self.assertEqual(result['broken_internal_references'], 1)
        self.assertEqual(result['duplicate_ids'], 1)
        self.assertEqual(result['media_files'], 1)

    def test_metadata_report_does_not_contain_manuscript_text(self):
        self.assertNotIn('PRIVATE SENTENCE', json.dumps(corpus.epub_structure(self.path)))

    def test_missing_epubcheck_is_not_a_validation_pass(self):
        result = corpus.epubcheck(self.path, str(Path(self.tmp.name) / 'missing.jar'), Path(self.tmp.name))
        self.assertEqual(result['status'], 'not_run')

    def test_diagnostics_do_not_export_exception_messages(self):
        try:
            raise RuntimeError('PRIVATE SENTENCE')
        except RuntimeError as exc:
            result = corpus.exception_metadata(exc)
        self.assertEqual(result['exception'], 'RuntimeError')
        self.assertNotIn('PRIVATE SENTENCE', json.dumps(result))

    def test_phase_deadline_is_not_swallowed_by_pipeline_fallback(self):
        self.assertFalse(issubclass(corpus.PhaseDeadline, Exception))

    def test_locators_use_reducer_content_and_allow_dot_relative_names(self):
        from bs4 import BeautifulSoup
        item = SimpleNamespace(get_name=lambda: './chapter.xhtml',
            get_content=lambda: b'<html><body><p>normalized</p></body></html>')
        book = SimpleNamespace(get_items=lambda: [item])
        manifest = {'chapters': [{'file_path': './chapter.xhtml', 'chapter_kind': 'body',
            'chunks': [{'chunk_id': 'c1', 'locator': '/html[1]/body[1]/p[1]',
                        'html': '<p>normalized</p>'}]}]}
        with patch('app.domain.manifest_service.build_manifest', return_value=manifest), \
             patch('app.engine.unpacker.EpubUnpacker.load_book', return_value=book), \
             patch('app.engine.cleaners.semantics_translator.SemanticsTranslator',
                   return_value=SimpleNamespace(_should_translate=lambda _: False)), \
             patch('bs4.BeautifulSoup', wraps=BeautifulSoup) as parse:
            result = corpus.manifest_checks(self.path, {'target_lang': 'zh-CN'})
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['locators_matched'], 1)
        self.assertEqual(parse.call_count, 1)


if __name__ == '__main__': unittest.main(verbosity=2)
