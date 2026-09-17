"""References are preserved without weakening real translation delivery gates."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from app.domain.translation_residual_policy import residual_category, is_preserved_reference
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_qa_service import audit_translated_epub_output, build_translation_qa_report
from app.job_runner import _apply_final_artifact_audit


class DeliveryPolicyTests(unittest.TestCase):
    def test_complete_references_are_exempt_in_every_layer(self):
        for value in ['www.panmacmillan.com', 'https://example.org/paper?year=2026', 'doi:10.1234/abcdefg']:
            self.assertTrue(is_preserved_reference(value))
            self.assertFalse(residual_category(value, source_text=value, title_like=True))
            audit = audit_translation_chunk(original_html=f'<p>{value}</p>', translated_html=f'<p>{value}</p>')
            self.assertEqual(audit.risk_level, 'ok')
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'book.epub'
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('chapter.xhtml', f'<html><body><h2>{value}</h2></body></html>')
                self.assertEqual(audit_translated_epub_output(path)['status'], 'passed')

    def test_urls_do_not_exempt_surrounding_prose_or_unsafe_schemes(self):
        for value in ['This is a long untranslated paragraph that mentions https://example.org and must be translated.',
                      'javascript:alert(1)', 'https://user:password@example.org', 'www.example.org this remains English']:
            self.assertFalse(is_preserved_reference(value))
        self.assertTrue(residual_category('This is a long untranslated paragraph that mentions https://example.org and must be translated.'))
        self.assertTrue(residual_category('LUKAS FRÖHLICH', source_text='LUKAS FRÖHLICH', title_like=True))

    def test_warning_does_not_block_delivery_but_failed_chunk_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'book.epub'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('chapter.xhtml', '<html><body><p>已经翻译的中文正文。</p></body></html>')
            job = SimpleNamespace(enable_translation=True, target_lang='zh-CN', bilingual=False, glossary={})
            for stats, expected in [({'audit_warn_chunks': 3}, True), ({'audit_failed_chunks': 1}, False), ({'failed_chunks': 1}, False)]:
                result = SimpleNamespace(validation_passed=True, translation_stats=stats, error_code=None, message='')
                _apply_final_artifact_audit(job, result, path)
                self.assertEqual(result.validation_passed, expected)
                self.assertEqual(result.translation_stats['deliverable'], expected)
                self.assertEqual(result.translation_stats['qa_report']['can_deliver'], expected)
                if not expected:
                    self.assertEqual(result.error_code, 'PARTIAL_TRANSLATION')

    def test_no_download_proof_is_claimed_before_artifact_audit(self):
        report = build_translation_qa_report(translation_stats={'audit_warn_chunks': 2})
        self.assertEqual(report['status'], 'warning')
        self.assertFalse(report['can_deliver'])
        self.assertEqual(report['delivery_status'], 'pending')


if __name__ == '__main__':
    unittest.main()
