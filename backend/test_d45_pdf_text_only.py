"""No-reference EPUB export must not embed page scans or leave dangling links."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup
from app.domain.pdf.pipeline import build_epub


class TextOnlyEpubTests(unittest.TestCase):
    def build(self, root, blocks, **page_options):
        pages = [{'page':30, 'label':'14', 'blocks':blocks, **page_options}]
        path = root/'sample.epub'
        report = build_epub(pages, root, path, '正文测试', language='zh-Hans', include_source_pages=False)
        return path, report

    def test_export_without_scans_preserves_body_and_navigation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # No page-30.jpg exists: export must neither require nor package it.
            path, report = self.build(root, [{'type':'heading','text':'标题与<符号>'}, {'type':'text','text':'第一段。\n\n第二段。'}])
            self.assertFalse(report['source_pages_included'])
            with zipfile.ZipFile(path) as book:
                self.assertFalse(any(n.endswith(('.jpg','.jpeg','.png','.pdf')) for n in book.namelist()))
                page = book.read('OEBPS/page-0030.xhtml').decode()
                self.assertNotIn('原页对照', page)
                self.assertNotIn('<figure', page)
                soup = BeautifulSoup(page, 'html.parser')
                self.assertEqual([p.get_text() for p in soup.find_all('p')], ['第一段。','第二段。'])
                self.assertEqual(soup.title.get_text(), '标题与<符号>')
                self.assertIn('page-0030.xhtml#pdf-page-30', book.read('OEBPS/nav.xhtml').decode())
                for name in book.namelist():
                    if name.endswith(('.xml','.xhtml','.opf')): ET.fromstring(book.read(name))

    def test_formula_fallback_does_not_link_to_an_omitted_scan(self):
        with tempfile.TemporaryDirectory() as d:
            path, report = self.build(Path(d), [{'type':'equation','text':r'\unknown{x}'},
                                               {'type':'equation','text':r'\sqrt{判别式}'},
                                               {'type':'text','text':r'分式 $\frac{1}{3}$。'}])
            self.assertEqual((report['svg'],report['mathml'],report['source_fallback']), (1,1,1))
            with zipfile.ZipFile(path) as book:
                page = book.read('OEBPS/page-0030.xhtml').decode()
                self.assertNotIn('#original-page', page)
                self.assertNotIn('原页', page)
                self.assertIn(r'\unknown{x}', page)
                self.assertIn('公式排版待核验', page)
                self.assertIn('<math ', page)
                self.assertEqual(sum(n.endswith('.svg') for n in book.namelist()), 1)

    def test_pending_page_remains_explicit_without_claiming_scan_present(self):
        with tempfile.TemporaryDirectory() as d:
            path, _ = self.build(Path(d), [], review_required=True)
            with zipfile.ZipFile(path) as book:
                page = book.read('OEBPS/page-0030.xhtml').decode()
                self.assertIn('OCR 内容待核验', page)
                self.assertNotIn('原页保留', page)


if __name__ == '__main__':
    unittest.main()
