"""Offline legacy PDF helpers; public product coverage lives in D59 and D37."""
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from app.domain.pdf import config, pipeline, providers
from test_d37_pdf_pipeline import fixture, response

class PdfServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = fixture(self.root / 'source.pdf')
        self.env = patch.dict(os.environ, {
            'PDF_WORK_DIR': str(self.root / 'private'), 'PDF_TRANSLATION_ENABLED':'1',
            'PDF_OCR_ENABLED':'1', 'PDF_DEEPSEEK_API_KEY':'offline-fixture', 'MINERU_API_KEY':'',
            'PDF_TRANSLATION_PRICE_PER_PAGE_CNY':'.20', 'PDF_OCR_PRICE_PER_PAGE_CNY':'.05',
            'PDF_OCR_MIN_PRICE_CNY':'1.99',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_independent_prices_and_gates(self):
        self.assertEqual(config.quote(100, 'literary', 'deepseek-pro', service='ocr')['price_cny'], '5.00')
        self.assertEqual(config.quote(100, service='translate')['price_cny'], '20.00')
        with patch.dict(os.environ, {'PDF_TRANSLATION_ENABLED': '0', 'PDF_TRANSLATION_PRICE_PER_PAGE_CNY': '0'}):
            caps = config.capabilities()
            self.assertTrue(caps['services']['ocr']['enabled'])
            self.assertFalse(caps['services']['translate']['enabled'])
            self.assertTrue(caps["services"]["ocr"]["enabled"])
        for value in ('0', '', 'NaN', '-1'):
            with patch.dict(os.environ, {'PDF_OCR_PRICE_PER_PAGE_CNY': value}):
                self.assertFalse(config.capabilities()['services']['ocr']['enabled'])
                with self.assertRaises(config.PdfError): config.quote(20, service='ocr')


    def test_approved_default_rates_and_minimum_boundary(self):
        with patch.dict(os.environ):
            os.environ.pop('PDF_OCR_PRICE_PER_PAGE_CNY', None)
            os.environ.pop('PDF_OCR_MIN_PRICE_CNY', None)
            for pages, expected in [(1, '1.99'), (20, '1.99'), (39, '1.99'),
                                    (40, '2.00'), (100, '5.00'), (300, '15.00')]:
                with self.subTest(pages=pages):
                    price = config.quote(pages, service='ocr')
                    self.assertEqual(price['price_cny'], expected)
                    self.assertEqual(price['per_page_cny'], '0.05')
                    self.assertEqual(price['minimum_cny'], '1.99')
            self.assertEqual(config.quote(20, service='translate')['price_cny'], '4.00')
            self.assertEqual(config.quote(300, service='translate')['price_cny'], '60.00')


    def test_legacy_translation_export_options_remain_unchanged(self):
        pf = pipeline.preflight(self.source, 'legacy')
        from app.domain.pdf.state import read, save, job_dir
        path = job_dir('legacy') / 'source.json'
        info = read(path)
        for key in ('service_mode', 'include_source_pages'):
            pf['pdf'].pop(key)
            info.pop(key)
        save(path, info)
        with patch.object(providers.Flash, 'call', return_value=response()):
            output, _ = pipeline.prepare(self.source, 'legacy', pf['pdf'])
        with zipfile.ZipFile(output) as book:
            self.assertIn('OEBPS/images/page-2.jpg', book.namelist())


if __name__ == '__main__': unittest.main()
