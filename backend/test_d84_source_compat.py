"""Deterministic source-markup repairs; never weaken link or input safety."""
import copy
import io
import unittest
import zipfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
import asyncio

from lxml import etree
from app.engine.html_compat import upgrade_legacy_html
import test_d64_metadata
from app.engine.epub_compat import normalize_package_structure, OPF_NS
from app.domain.epub_input_integrity import validate_epub_resources, EpubInputError
from app.engine.font_compat import repair_font_sources
from bs4 import BeautifulSoup


class SourceMarkupTests(unittest.TestCase):
    def tree(self, nested=None):
        nested = nested or '<a id="back1" href="#r1"><sup><a id="back2" href="#r2"><sup>*1</sup></a></sup></a>'
        return etree.fromstring(('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title></head>'
            '<body><p>First<a id="r1" href="#note">*1</a></p>'
            '<p>Second<a id="r2" href="#note">*1</a></p>'
            '<p id="note">' + nested + ' Note prose and tail.</p></body></html>').encode())

    def test_both_return_destinations_ids_and_prose_survive(self):
        root = self.tree()
        before_text = ''.join(root.itertext())
        before_links = [(n.get('id'), n.get('href')) for n in root.xpath('//*[local-name()="a"]')]
        upgrade_legacy_html(root)
        self.assertFalse(root.xpath('//*[local-name()="a"]//*[local-name()="a"]'))
        self.assertEqual(before_links, [(n.get('id'), n.get('href')) for n in root.xpath('//*[local-name()="a"]')])
        marker = root.xpath('//*[@data-epub-compat-backlink-label]')
        self.assertEqual(len(marker), 1)
        self.assertEqual(marker[0].getparent().get('id'), 'back1')
        preserved = copy.deepcopy(root)
        preserved.xpath('//*[@data-epub-compat-backlink-label]')[0].text = ''
        self.assertEqual(''.join(preserved.itertext()), before_text)
        after = etree.tostring(root)
        upgrade_legacy_html(root)
        self.assertEqual(etree.tostring(root), after)

    def test_ambiguous_or_non_note_nested_links_are_not_guessed(self):
        mutations = [lambda r: r.xpath('//*[@id="r2"]')[0].set('href', '#other'),
            lambda r: r.xpath('//*[@id="back2"]')[0].set('href', 'https://example.invalid/#r2'),
            lambda r: r.xpath('//*[@id="back2"]')[0].set('href', 'https://[invalid/#r2'),
            lambda r: r.xpath('//*[@id="back2"]')[0].set('href', '#missing'),
            lambda r: r.xpath('//*[@id="back2"]')[0][0].__setattr__('text', 'Actual prose'),
            lambda r: r.xpath('//*[@id="r2"]')[0].set('id', 'r1'),
            lambda r: r.xpath('//*[@id="back1"]')[0].__setattr__('text', 'Separate prose'),
            lambda r: r.xpath('//*[@id="back2"]')[0].set('href', '#r1')]
        for mutate in mutations:
            root = self.tree(); mutate(root); before = etree.tostring(root)
            upgrade_legacy_html(root)
            self.assertEqual(etree.tostring(root), before)

    def test_orphan_head_metadata_retains_value_id_and_is_idempotent(self):
        helper = test_d64_metadata.EncodingDeclarationTests()
        _, chapter = helper.book('<meta id="legacy" content="text/html; charset=UTF-8"/>'
            '<meta id="author" name="author" content="A Writer"/>')
        for _ in range(2):
            chapter.content = chapter.get_content()
            node = helper.by_id(chapter.content, 'legacy')
            self.assertEqual(node.get('content'), 'text/html; charset=UTF-8')
            self.assertEqual(node.get('name'), 'epub-factory-legacy-content')
            self.assertEqual(helper.by_id(chapter.content, 'author').get('content'), 'A Writer')


class LegacyPackageTests(unittest.TestCase):
    def package(self, namespace='', extra='', href='chapter.xhtml', spine_extra=''):
        return (f'<package {namespace} version="2.0" unique-identifier="book-id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="book-id">fixture</dc:identifier><dc:title>Fixture</dc:title>'
            '<dc:language>zh</dc:language><meta name="legacy" content="preserved"/></metadata>'
            '<manifest><!-- resource list --><?editor harmless?>' + extra +
            f'<item id="chapter" href="{href}" media-type="application/xhtml+xml"/>'
            '</manifest><spine><!-- reading order -->' + spine_extra +
            '<itemref idref="chapter"/></spine></package>').encode()

    def archive(self, raw):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr('content.opf', raw)
            z.writestr('chapter.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Fixture</title></head><body><p id="a">Original prose.</p></body></html>')
        stream.seek(0)
        return stream

    def test_old_namespace_and_resource_comments_share_preflight_path(self):
        for ns in ('', f'xmlns="{OPF_NS}"'):
            raw = self.package(ns)
            stream = self.archive(raw)
            before = stream.getvalue()
            self.assertEqual(validate_epub_resources(stream), [])
            self.assertEqual(stream.getvalue(), before)
            root = etree.fromstring(raw)
            self.assertTrue(normalize_package_structure(root))
            self.assertFalse(normalize_package_structure(root))
            self.assertEqual(root.find('{*}manifest')[0].get('href'), 'chapter.xhtml')
            self.assertEqual(root.find('{*}metadata/{*}meta').get('content'), 'preserved')
            self.assertEqual(root.find('{*}metadata/{http://purl.org/dc/elements/1.1/}title').text, 'Fixture')

    def test_unsafe_ambiguous_and_foreign_declarations_still_rejected(self):
        cases = [self.package(href='../escape.xhtml'), self.package(href='%2e%2e/escape.xhtml'),
                 self.package(extra='<item id="chapter" href="chapter.xhtml"/>'),
                 self.package(extra='<x:item xmlns:x="urn:foreign" id="x" href="chapter.xhtml"/>'),
                 self.package(spine_extra='<x:itemref xmlns:x="urn:foreign" idref="chapter"/>'),
                 self.package(namespace='xmlns="urn:foreign"')]
        for raw in cases:
            with self.assertRaises(EpubInputError):
                validate_epub_resources(self.archive(raw))

    def test_identical_unreferenced_stylesheet_alias_only(self):
        css = '<item id="style-a" href="main.css" media-type="text/css"/><item id="style-b" href="main.css" media-type="text/css"/>'
        root = etree.fromstring(self.package(extra=css))
        self.assertTrue(normalize_package_structure(root))
        self.assertEqual(len(root.find('{*}manifest')), 2)
        self.assertEqual(root.find('{*}manifest')[0].get('id'), 'style-a')
        self.assertFalse(normalize_package_structure(root))
        for extra in [css.replace('id="style-b"', 'id="style-a"'),
                      css.replace('id="style-b"', 'properties="remote-resources" id="style-b"'),
                      css.replace('text/css', 'application/xhtml+xml')]:
            with self.assertRaises(ValueError):
                normalize_package_structure(etree.fromstring(self.package(extra=extra)))
        root = etree.fromstring(self.package(extra=css))
        etree.SubElement(root.find('metadata'), 'meta', refines='#style-b')
        with self.assertRaises(ValueError): normalize_package_structure(root)

    def test_legacy_metadata_repairs_without_inventing_isbn(self):
        helper = test_d64_metadata.EmptyDescriptionTests()
        nodes = '<dc:identifier opf:scheme="ISBN"/><dc:language>简体中文</dc:language><dc:subject/><dc:coverage/>'
        book, raw = helper.normalize(nodes)
        self.assertEqual(book.language, 'zh-Hans')
        value, attrs = book.metadata[test_d64_metadata.DC_NS]['identifier'][0]
        self.assertTrue(value.startswith('urn:sha256:'))
        self.assertEqual(attrs['id'], book.IDENTIFIER_ID)
        self.assertEqual(book.uid, value)
        metas = book.metadata[OPF_NS]['meta']
        self.assertTrue(any(a.get('name') == 'legacy-dc-language' and a.get('content') == '简体中文' for _, a in metas))
        self.assertTrue(any(a.get('name') == 'legacy-empty-identifier-scheme' and a.get('content') == 'ISBN' for _, a in metas))
        self.assertNotIn('subject', book.metadata[test_d64_metadata.DC_NS])
        again, _ = helper.normalize(nodes)
        self.assertEqual(book.metadata, again.metadata)
        valid, _ = helper.normalize('<dc:identifier id="keep">Original-ID</dc:identifier><dc:language>en</dc:language>')
        self.assertEqual((valid.IDENTIFIER_ID, valid.uid), ('keep', 'Original-ID'))
        anonymous, _ = helper.normalize('<dc:identifier>Existing-ID</dc:identifier>'
            '<meta id="epub-factory-identifier" name="fixture" content="keep"/>')
        self.assertEqual(anonymous.uid, 'Existing-ID')
        self.assertEqual(anonymous.IDENTIFIER_ID, 'epub-factory-identifier-compat')

    def test_reader_font_uri_keeps_embedded_and_local_alternatives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); font = root / 'embedded.ttf'; font.write_bytes(b'font fixture')
            css = '@font-face {font-family:Book;src:url(res:///sony/font.ttf),local(serif),url(embedded.ttf)} body{font-family:Book,serif}'
            fixed, count = repair_font_sources(css, root / 'main.css', root)
            self.assertEqual(count, 1)
            self.assertNotIn('res:///', fixed)
            self.assertIn('embedded.ttf', fixed)
            self.assertIn('local(serif)', fixed)
            self.assertIn('body{font-family:Book,serif}', fixed)
            self.assertEqual(font.read_bytes(), b'font fixture')
            for uri in ('https://example.invalid/font.ttf', 'data:font/ttf;base64,AAAA'):
                css = '@font-face {font-family:Book;src:url("' + uri + '")}'
                self.assertEqual(repair_font_sources(css, root / 'main.css', root), (css, 0))


class BilingualContentModelTests(unittest.TestCase):
    def test_reduce_flow_and_phrasing_wrappers_preserve_tables_and_anchors(self):
        from app.domain.chapter_reduce_service import apply_chunk_results
        for tag in ('div', 'li', 'blockquote', 'p', 'h1'):
            flow = tag in {'div', 'li', 'blockquote'}
            inner = '<table><tbody><tr><td id="cell">Hello world</td></tr></tbody></table>' if flow else '<a id="origin" href="#target">Hello world</a>'
            html = f'<html><body><{tag}>{inner}</{tag}></body></html>'.encode()
            row = SimpleNamespace(locator=f'/html/body/{tag}', translated_html=inner.replace('Hello world', '你好世界'), sequence=0, chunk_id='fixture')
            output = BeautifulSoup(apply_chunk_results(html, [row], True), 'html.parser')
            original, translated = output.select_one('.epub-original'), output.select_one('.epub-translated')
            self.assertEqual(original.name, 'div' if flow else 'span')
            self.assertEqual(translated.name, original.name)
            self.assertEqual(original.get_text(), 'Hello world')
            self.assertEqual(translated.get_text(), '你好世界')
            self.assertEqual(len(output.find_all(id=True)), 1)
            self.assertFalse(output.select('span table'))

    def test_legacy_translator_uses_same_flow_wrapper(self):
        import test_c4_bilingual
        translator = test_c4_bilingual.make_translator(True)
        html = b'<html><body><div><table><tbody><tr><td>Hello world</td></tr></tbody></table></div></body></html>'
        output = BeautifulSoup(asyncio.run(translator.process_async(html, 9)), 'html.parser')
        self.assertEqual(output.select_one('.epub-original').name, 'div')
        self.assertEqual(output.select_one('.epub-translated').name, 'div')
        self.assertEqual(len(output.select('div table')), 2)
        self.assertFalse(output.select('span table'))


if __name__ == '__main__':
    unittest.main()
