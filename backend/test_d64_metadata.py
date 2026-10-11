"""Legacy metadata compatibility without removing author content."""
from pathlib import Path
import tempfile
import unittest
import zipfile

from ebooklib import epub
from lxml import etree

from app.engine.epub_compat import normalize_book, normalize_metadata, DC_NS, OPF_NS
from app.engine.packager import EpubPackager
from app.engine.unpacker import EpubUnpacker


class EncodingDeclarationTests(unittest.TestCase):
    def book(self, head):
        book = epub.EpubBook()
        book.set_identifier('d64-metadata')
        book.set_title('Fixture')
        book.set_language('en')
        chapter = epub.EpubHtml(uid='chapter', file_name='chapter.xhtml', title='Fixture')
        chapter.content = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Fixture</title>'
                           + head + '</head><body><p id="start">Unchanged text.</p></body></html>').encode()
        book.add_item(chapter)
        book.spine = [chapter]
        book.toc = [epub.Link('chapter.xhtml#start', 'Fixture', 'toc')]
        normalize_book(book, 'content.opf')
        return book, chapter

    def by_id(self, raw, identifier):
        return etree.fromstring(raw).xpath('//*[@id=$id]', id=identifier)[0]

    def test_legacy_mime_and_case_normalize_to_actual_output_encoding(self):
        for value in ('Content-Type', ' content-type ', 'CONTENT-TYPE'):
            with self.subTest(value=value):
                _, chapter = self.book('<meta id="encoding" http-equiv="' + value +
                    '" content="http://www.w3.org/1999/xhtml; charset=gbk"/>')
                node = self.by_id(chapter.get_content(), 'encoding')
                self.assertEqual(node.get('http-equiv'), 'content-type')
                self.assertEqual(node.get('content'), 'text/html; charset=utf-8')

    def test_standalone_charset_follows_utf8_serialization(self):
        _, chapter = self.book('<meta id="encoding" charset="gb2312"/>')
        self.assertEqual(self.by_id(chapter.get_content(), 'encoding').get('charset'), 'utf-8')

    def test_author_metadata_and_conflicting_semantics_are_not_rewritten(self):
        for attrs in ('name="author" content="charset=gbk"',
                      'property="schema:encoding" content="charset=gbk"',
                      'name="custom" http-equiv="content-type" content="author value"',
                      'http-equiv="content-type" charset="gbk" content="conflicting"'):
            with self.subTest(attrs=attrs):
                _, chapter = self.book('<meta id="custom" ' + attrs + '/>')
                before = self.by_id(chapter.content, 'custom')
                after = self.by_id(chapter.get_content(), 'custom')
                self.assertEqual(dict(after.attrib), dict(before.attrib))

    def test_foreign_namespace_and_non_head_meta_are_not_encoding_declarations(self):
        _, chapter = self.book('<foreign:meta xmlns:foreign="urn:fixture" id="foreign" '
                               'http-equiv="content-type" content="original"/>')
        chapter.content = chapter.content.replace(b'</body>',
            b'<div><meta id="body-meta" charset="original"/></div></body>')
        # Hold the underlying writer output fixed to isolate this compatibility
        # layer from ebooklib's separate foreign-markup parsing behavior.
        chapter._epub_factory_original_get_content = lambda default=None: chapter.content
        raw = chapter.get_content()
        self.assertEqual(self.by_id(raw, 'foreign').get('content'), 'original')
        self.assertEqual(self.by_id(raw, 'body-meta').get('charset'), 'original')

    def test_head_payloads_ids_order_and_repeated_roundtrip_are_preserved(self):
        head = ('<meta id="before" name="author" content="Fixture"/>'
                '<meta id="encoding" http-equiv="content-type" content="invalid; charset=gbk"/>'
                '<style id="style">p::after {content: "a&amp;b";}</style>'
                '<script id="script">if (a &amp;&amp; b) { x = "a&amp;b"; }</script>'
                '<meta id="after" name="custom" content="value"/>')
        book, chapter = self.book(head)
        def snapshot(raw):
            root = etree.fromstring(raw)
            return [(n.get('id'), dict(n.attrib), n.text) for n in root.xpath('//*[local-name()="head"]/*')
                    if n.get('id') != 'encoding']
        expected = snapshot(chapter.content)
        ids = ['before', 'encoding', 'style', 'script', 'after']
        for _ in range(3):
            chapter.content = chapter.get_content()
            self.assertEqual(snapshot(chapter.content), expected)
            self.assertEqual(etree.fromstring(chapter.content).xpath('//*[local-name()="head"]/*/@id'), ids)
            self.assertEqual(self.by_id(chapter.content, 'encoding').get('content'), 'text/html; charset=utf-8')
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / 'first.epub', Path(tmp) / 'second.epub'
            self.assertTrue(EpubPackager(book, first).save())
            restored = EpubUnpacker(first).load_book()
            self.assertIsNotNone(restored)
            self.assertTrue(EpubPackager(restored, second).save())
            with zipfile.ZipFile(second) as archive:
                raw = archive.read('EPUB/chapter.xhtml')
                self.assertEqual(snapshot(raw), expected)
                self.assertEqual(self.by_id(raw, 'encoding').get('content'), 'text/html; charset=utf-8')


class EmptyDescriptionTests(unittest.TestCase):
    def normalize(self, nodes):
        raw = ('<package xmlns="http://www.idpf.org/2007/opf" prefix="schema: http://schema.org/">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
               'xmlns:opf="http://www.idpf.org/2007/opf">' + nodes + '</metadata></package>').encode()
        book = epub.EpubBook()
        normalize_metadata(book, raw)
        return book, raw

    def test_only_unadorned_empty_optional_description_is_omitted(self):
        for content in ('', ' \t\r\n'):
            with self.subTest(content=content):
                book, _ = self.normalize('<dc:description>' + content + '</dc:description>')
                self.assertNotIn('description', book.metadata[DC_NS])

    def test_nonempty_description_and_unicode_whitespace_are_preserved(self):
        for text in ('Original description.', '\u00a0'):
            with self.subTest(text=text):
                book, _ = self.normalize('<dc:description>' + text + '</dc:description>')
                self.assertEqual(book.metadata[DC_NS]['description'], [(text, {})])

    def test_empty_description_with_identity_and_refinement_is_not_removed(self):
        book, _ = self.normalize('<dc:description id="desc"/>'
            '<meta refines="#desc" property="alternate-script" xml:lang="en">Retained.</meta>')
        self.assertEqual(book.metadata[DC_NS]['description'], [(None, {'id': 'desc'})])
        value, attrs = book.metadata[OPF_NS]['meta'][0]
        self.assertEqual(value, 'Retained.')
        self.assertEqual(attrs['refines'], '#desc')

    def test_description_attributes_children_and_required_fields_are_not_hidden(self):
        for node in ('<dc:description xml:lang="en"/>', '<dc:description dir="ltr"/>',
                     '<dc:description opf:custom="value"/>',
                     '<dc:description><span xmlns="urn:fixture">Retain evidence</span></dc:description>'):
            with self.subTest(node=node):
                book, _ = self.normalize(node + '<dc:title/><dc:identifier/><dc:language/>')
                self.assertIn('description', book.metadata[DC_NS])
                for required in ('title', 'identifier', 'language'):
                    self.assertIn(required, book.metadata[DC_NS])

    def test_metadata_identity_refinements_and_repeated_normalization_are_stable(self):
        book, raw = self.normalize('<dc:description/>'
            '<dc:creator opf:role="aut" opf:file-as="Writer, A">A Writer</dc:creator>'
            '<dc:identifier id="uid" opf:scheme="ISBN">123</dc:identifier>'
            '<meta property="schema:accessMode">textual</meta>')
        before = repr(book.metadata)
        self.assertEqual(book.metadata[DC_NS]['creator'][0][1]['id'], 'legacy-meta-1')
        metas = book.metadata[OPF_NS]['meta']
        self.assertTrue(any(v == 'aut' and a.get('refines') == '#legacy-meta-1' for v, a in metas))
        self.assertTrue(any(v == 'ISBN' and a.get('refines') == '#uid' for v, a in metas))
        normalize_metadata(book, raw)
        self.assertEqual(repr(book.metadata), before)


if __name__ == '__main__':
    unittest.main()
