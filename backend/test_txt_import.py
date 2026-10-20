"""Synthetic Unicode inputs only; no network or provider requests."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from lxml import etree

with patch('dotenv.load_dotenv', return_value=False):
    from app.engine.adapters.txt_adapter import txt_to_html, decode_txt
    from app.domain.translation_input import normalized_translation_input


class TxtImportTests(unittest.TestCase):
    def test_five_languages_headings_text_and_navigation(self):
        for language, content in [('en','Chapter 1\nThis is a book.'),('ja','第1章\nこれは図書館の本です。'),
                ('ko','제1장\n한국어 책입니다.'),('zh-CN','第一章\n这是简体书籍。'),('zh-TW','第一章\n這是繁體書籍。')]:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'book.txt';path.write_text(content)
                html,meta=txt_to_html(path)
                self.assertEqual(meta['language'],language)
                self.assertEqual(len(meta['toc']),1)
                self.assertIn(content.split('\n')[1],html)
                with normalized_translation_input(path) as normalized:
                    self.assertEqual(normalized.adapter,'txt')
                    first=normalized.epub_path.read_bytes()
                    with zipfile.ZipFile(normalized.epub_path) as archive:
                        nav=archive.read('OEBPS/nav.xhtml')
                        self.assertIn(b'#txt-chapter-1',nav)
                        self.assertIn(content.split('\n')[1].encode(),archive.read('OEBPS/chapter1.xhtml'))
                with normalized_translation_input(path) as normalized:
                    self.assertEqual(first,normalized.epub_path.read_bytes())

    def test_text_is_escaped_not_executed_or_parsed_as_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'book.txt';path.write_text('<script>alert(1)</script>\n![x](https://evil.test) & text')
            html,meta=txt_to_html(path)
            self.assertNotIn('<script>',html)
            self.assertNotIn('<img',html)
            root=etree.fromstring(('<body>'+html+'</body>').encode())
            self.assertIn('<script>alert(1)</script>', ''.join(root.itertext()))

    def test_encoding_failures_and_bom(self):
        for data in [b'',b'  \n',b'\xff\x80',b'hello\x00world']:
            with self.assertRaises(ValueError):decode_txt(data)
        for encoding in ['utf-8-sig','utf-16']:
            self.assertEqual(decode_txt('繁體 日本語 한국어 English'.encode(encoding)),'繁體 日本語 한국어 English')

    def test_chapter_detection_does_not_discard_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'book.txt';path.write_text('A preface\n\nChapter 1: Start\nFirst line\nSecond line\n\nChapter 2\nEnd')
            html,meta=txt_to_html(path)
            self.assertEqual(len(meta['toc']),2)
            for text in ['A preface','First line','Second line','End']:self.assertIn(text,html)


if __name__=='__main__':unittest.main()
