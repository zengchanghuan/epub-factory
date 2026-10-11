"""Physical ZIP paths and serialized OPF URIs must not be conflated."""
from pathlib import Path
import hashlib
import tempfile
import unittest
from urllib.parse import quote, unquote, urlsplit
import zipfile

from ebooklib import epub
from lxml import etree

from app.engine.packager import EpubPackager
from app.engine.unpacker import EpubUnpacker


class PackageUriTests(unittest.TestCase):
    def package(self, directory, name, svg=False, embedded_opf=False):
        book = epub.EpubBook()
        book.set_identifier('d64-uri-fixture')
        book.set_title('URI fixture')
        book.set_language('en')
        chapter = epub.EpubHtml(uid='chapter', file_name=name, title='Chapter')
        chapter.content = '<html><head><title>Chapter</title></head><body><p id="start">Unchanged text.</p></body></html>'
        if svg:
            chapter.content = chapter.content.replace('</body>', '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="2"/></svg></body>')
        image_name = 'Images/a %20 # ?.png'
        image = epub.EpubItem(uid='image', file_name=image_name, media_type='image/png', content=b'unchanged-test-image')
        book.add_item(chapter)
        book.add_item(image)
        if embedded_opf:
            book.add_item(epub.EpubItem(uid='secondary', file_name='secondary.opf',
                media_type='application/oebps-package+xml',
                content=b'<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="chapter" href="unrelated.xhtml" media-type="application/xhtml+xml"/></manifest></package>'))
        book.spine = [chapter]
        book.toc = [epub.Link(quote(name, safe='/') + '#start', 'Chapter', 'toc-1')]
        out = directory / 'output.epub'
        self.assertTrue(EpubPackager(book, out).save())
        return out, book

    def assert_manifest(self, output, book):
        with zipfile.ZipFile(output) as archive:
            rootfile = etree.fromstring(archive.read('META-INF/container.xml')).find('.//{*}rootfile')
            opf_name = rootfile.get('full-path')
            opf = etree.fromstring(archive.read(opf_name))
            base = Path(opf_name).parent.as_posix()
            for node in opf.findall('.//{*}manifest/{*}item'):
                item = book.get_item_with_id(node.get('id'))
                self.assertIsNotNone(item)
                self.assertEqual(node.get('href'), quote(item.file_name, safe='/'))
                uri = urlsplit(node.get('href'))
                self.assertFalse(uri.query or uri.fragment)
                self.assertIn(base + '/' + unquote(uri.path), archive.namelist())
            self.assertEqual(archive.read(base + '/Images/a %20 # ?.png'), b'unchanged-test-image')
            self.assertEqual(archive.namelist()[0], 'mimetype')
            self.assertEqual(archive.getinfo('mimetype').compress_type, zipfile.ZIP_STORED)

    def test_manifest_quotes_physical_paths_without_renaming_members(self):
        for name in ('Text/chapter one.xhtml', 'Text/chapter%20two.xhtml',
                     'Text/chapter#three?.xhtml', 'Text/章节.xhtml', 'Text/plain.xhtml'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                output, book = self.package(Path(tmp), name)
                self.assert_manifest(output, book)

    def test_svg_declaration_survives_encoded_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, book = self.package(Path(tmp), 'Text/svg chapter.xhtml', svg=True)
            self.assert_manifest(output, book)
            with zipfile.ZipFile(output) as archive:
                opf = etree.fromstring(archive.read('EPUB/content.opf'))
                item = opf.find('.//{*}item[@id="chapter"]')
                self.assertIn('svg', item.get('properties', '').split())

    def test_embedded_opf_is_not_retargeted_using_primary_manifest_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, book = self.package(Path(tmp), 'Text/chapter one.xhtml', embedded_opf=True)
            self.assert_manifest(output, book)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read('EPUB/secondary.opf'), book.get_item_with_id('secondary').content)

    def test_already_encoded_input_roundtrips_without_double_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / 'source.epub'
            physical = 'Text/chapter%20literal space.xhtml'
            raw_html = b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title></head><body><p id="a">Keep text.</p></body></html>'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
                archive.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
                archive.writestr('content.opf', '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">test</dc:identifier><dc:title>One</dc:title><dc:language>en</dc:language></metadata><manifest><item id="ch" href="' + quote(physical, safe='/') + '" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="ch"/></spine></package>')
                archive.writestr(physical, raw_html)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            book = EpubUnpacker(source).load_book()
            self.assertIsNotNone(book)
            self.assertEqual(book.get_item_with_id('ch').file_name, physical)
            output = directory / 'out.epub'
            self.assertTrue(EpubPackager(book, output).save())
            with zipfile.ZipFile(output) as archive:
                opf = etree.fromstring(archive.read('EPUB/content.opf'))
                item = opf.find('.//{*}item[@id="ch"]')
                self.assertEqual(item.get('href'), quote(physical, safe='/'))
                self.assertIn('EPUB/' + physical, archive.namelist())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
