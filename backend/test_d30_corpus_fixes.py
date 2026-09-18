"""Real-corpus defects expressed as portable, private-text-free fixtures."""
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ebooklib import epub
from fastapi import UploadFile
from fastapi.testclient import TestClient
from lxml import etree
from bs4 import BeautifulSoup

from app.domain.manifest_service import build_manifest, classify_chapter_kind
from app.domain.translation_residual_policy import residual_category
from app.domain.translation_qa_service import audit_translated_epub_output
from app.engine.unpacker import EpubUnpacker
from app.engine.packager import EpubPackager
from app.engine.epub_compat import normalize_metadata, DC_NS, OPF_NS
from app.engine.font_compat import repair_font_sources
from app.engine.html_compat import upgrade_legacy_html
from app.engine.adapters import html_to_epub_builder
from app.engine.cleaners.device_profile import DeviceProfileCompiler
from app.engine.compiler import ExtremeCompiler
from app.engine.cleaners.cjk_normalizer import CjkNormalizer
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.main import app, _validate_upload_format
from app.models import ChapterKind, OutputMode
from app.engine.chunk_extractor import extract_chunks, _build_locator
from app.converter import EpubConverter


def fixture(path, *, outside=False, html_type='application/xhtml+xml', ncx=False, bad_ncx=False,
            missing_body=False, missing_page_map=False):
    relative = '../' if outside else ''
    chapter = relative + 'chapter.xhtml'
    nav = relative + 'nav.xhtml'
    items = f'<item id="ch" href="{chapter}" media-type="{html_type}"/>' \
            f'<item id="nav" href="{nav}" media-type="application/xhtml+xml" properties="nav"/>'
    if ncx: items += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    if missing_page_map: items += '<item id="pm" href="page-map.xml" media-type="application/oebps-page-map+xml"/>'
    spine_attrs = (' toc="ncx"' if ncx else '') + (' page-map="pm"' if missing_page_map else '')
    opf = f'''<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">fixture</dc:identifier>
      <dc:title>Fixture</dc:title><dc:language>zh</dc:language></metadata>
      <manifest>{items}</manifest><spine{spine_attrs}><itemref idref="ch"/></spine></package>'''
    nav_target = 'chapter.xhtml#start'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('OEBPS/content.opf', opf)
        if not missing_body:
            z.writestr(('' if outside else 'OEBPS/') + 'chapter.xhtml',
                '<html xmlns="http://www.w3.org/1999/xhtml" id="root-anchor"><head><title>Fixture</title>'
                '<style>p { margin: 1em; }</style></head><body id="start" class="chapter">'
                'Leading text.<p>这是正文。</p></body></html>')
        z.writestr(('' if outside else 'OEBPS/') + 'nav.xhtml',
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目录</title></head>'
            f'<body><nav epub:type="toc"><ol><li><a href="{nav_target}">章节</a></li></ol></nav></body></html>')
        if ncx:
            target = 'missing.xhtml' if bad_ncx else chapter + '#start'
            z.writestr('OEBPS/toc.ncx', '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="p">'
                f'<navLabel><text>章节</text></navLabel><content src="{target}"/></navPoint></navMap></ncx>')


class CorpusFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.source = self.root / 'source.epub'

    def test_pdf_upload_routes_reject_before_orders_and_payment(self):
        client = TestClient(app)
        fixture(self.source)
        valid_epub = self.source.read_bytes()
        self.source.unlink()
        for endpoint, field, uploads in [('/api/v1/jobs', 'file', [('book.PDF', b'%PDF-1.7')]),
            ('/api/v2/jobs', 'file', [('book.pdf', b'%PDF-1.7')]),
            ('/api/v2/batches', 'files', [('valid.epub', valid_epub), ('book.pdf', b'%PDF-1.7')])]:
            with self.subTest(endpoint=endpoint), patch('app.main.UPLOAD_DIR', self.root), \
                    patch('app.main.job_store.add') as add, patch('app.main.create_alipay_page_pay') as pay:
                response = client.post(endpoint, files=[(field, (name, data)) for name, data in uploads])
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn('暂不支持 PDF', response.json()['detail'])
                add.assert_not_called(); pay.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_pdf_renamed_epub_is_rejected(self):
        upload = UploadFile(filename='book.epub', file=io.BytesIO(b'%PDF-1.7\ncontent'))
        with self.assertRaises(Exception) as result: _validate_upload_format(upload)
        self.assertEqual(result.exception.status_code, 400)
        self.assertEqual(upload.file.tell(), 0)

    def test_pdf_existing_job_conversion_is_disabled(self):
        with self.assertRaisesRegex(RuntimeError, '暂不支持 PDF'):
            EpubConverter().convert_file_to_horizontal(self.root / 'old.pdf', self.root / 'out.epub', OutputMode.simplified)

    def test_valid_upload_peek_keeps_file_position(self):
        fixture(self.source)
        source_bytes = self.source.read_bytes()
        upload = UploadFile(filename='book.epub', file=io.BytesIO(source_bytes))
        _validate_upload_format(upload)
        self.assertEqual(upload.file.read(), source_bytes)

    def test_supported_adapter_builder_has_real_nav_and_readable_body(self):
        html_to_epub_builder.build('<p>这是正文。</p>', {'title': '样书', 'author': 'A & B', 'identifier': 'id<&'}, self.source)
        manifest = build_manifest(str(self.source), 'fixture')
        self.assertNotIn('error', manifest)
        self.assertEqual(sum(len(ch['chunks']) for ch in manifest['chapters'] if ch['chapter_kind'] == 'body'), 1)
        self.assertEqual(sum(ch['chapter_kind'] == 'nav' for ch in manifest['chapters']), 1)
        audit = audit_translated_epub_output(self.source)
        self.assertEqual(audit['navigation_broken_targets'], 0)
        self.assertEqual(audit['status'], 'passed')
        with zipfile.ZipFile(self.source) as archive:
            package = etree.fromstring(archive.read('OEBPS/content.opf'))
            self.assertTrue(package.xpath('//*[local-name()="meta"][@property="dcterms:modified"]'))
            self.assertFalse(package.xpath('//*[local-name()="item"][@id="chapter1"][@properties]'))

    def test_generic_split_files_and_keyword_substrings_are_body(self):
        for name in ['index_split_002.html', 'Text/index_split_003.html', 'navy.xhtml',
                     'discovery.xhtml', 'navigation-history/chapter.xhtml']:
            self.assertEqual(classify_chapter_kind(name), ChapterKind.body, name)
            self.assertFalse(ExtremeCompiler._should_skip_translation_for_file(name))
        for name in ['TableOfContents.xhtml', 'table-of-contents.xhtml', 'table_of_contents.xhtml']:
            self.assertEqual(classify_chapter_kind(name), ChapterKind.nav)
            self.assertTrue(ExtremeCompiler._should_skip_translation_for_file(name))

    def test_nav_item_metadata_wins_over_generic_file_name(self):
        fixture(self.source)
        with zipfile.ZipFile(self.source) as z:
            entries = [(i, z.read(i.filename).replace(b'nav.xhtml', b'ordinary.xhtml')) for i in z.infolist()]
        with zipfile.ZipFile(self.source, 'w') as z:
            for info, raw in entries:
                name = info.filename.replace('nav.xhtml', 'ordinary.xhtml')
                z.writestr(name, raw)
        manifest = build_manifest(str(self.source), 'fixture')
        nav = next(ch for ch in manifest['chapters'] if ch['file_path'] == 'ordinary.xhtml')
        self.assertEqual(nav['chapter_kind'], 'nav')

    def test_japanese_unicode_and_chinese_targets(self):
        t = SemanticsTranslator.__new__(SemanticsTranslator)
        t.target_lang = 'zh-CN'
        for text in ['これは本文です。', '語り手が話している。', 'Пример текста', 'مرحبا بالعالم', 'café déjà vu']:
            self.assertTrue(t._should_translate(text), text)
            self.assertTrue(t._should_translate_text_node(text), text)
        for text in ['这是中文正文。', '12345', '   ']: self.assertFalse(t._should_translate(text))

    def test_generic_nav_is_not_body_but_labels_still_need_translation(self):
        fixture(self.source)
        with zipfile.ZipFile(self.source) as z:
            entries = [(i.filename.replace('nav.xhtml', 'ordinary.xhtml'),
                        z.read(i.filename).replace(b'nav.xhtml', b'ordinary.xhtml')) for i in z.infolist()]
        with zipfile.ZipFile(self.source, 'w') as z:
            for name, raw in entries:
                if name.endswith('ordinary.xhtml'):
                    raw = raw.replace(b'<body>', b'<body><h1>Table of Contents</h1>')
                z.writestr(name, raw)
        audit = audit_translated_epub_output(self.source)
        self.assertEqual(audit['non_body_files_skipped'], 1)
        self.assertEqual(audit['residual_blocks'], 0)
        self.assertEqual(audit['status'], 'passed')
        with zipfile.ZipFile(self.source) as z:
            entries = [(i.filename, z.read(i.filename)) for i in z.infolist()]
        with zipfile.ZipFile(self.source, 'w') as z:
            for name, raw in entries:
                if name.endswith('ordinary.xhtml'):
                    raw = raw.replace('章节'.encode(), b'Chapter One')
                z.writestr(name, raw)
        audit = audit_translated_epub_output(self.source)
        self.assertEqual(audit['residual_blocks'], 0)
        self.assertEqual(audit['navigation_residual_labels'], 1)
        self.assertEqual(audit['status'], 'failed')

    def test_secondary_named_html_toc_is_audited_independently_of_body(self):
        fixture(self.source)
        with zipfile.ZipFile(self.source, 'a') as z:
            z.writestr('OEBPS/TableOfContents.xhtml',
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="chapter.xhtml#start">Chapter One</a></li></ol></nav></body></html>')
        audit = audit_translated_epub_output(self.source)
        self.assertEqual(audit['residual_blocks'], 0)
        self.assertEqual(audit['navigation_residual_labels'], 1)
        self.assertEqual(audit['navigation_broken_targets'], 0)
        self.assertEqual(audit['status'], 'failed')

    def test_japanese_residual_and_explicit_preservation(self):
        self.assertTrue(residual_category('これは本文です。'))
        self.assertTrue(residual_category('はい', source_text='はい'))
        self.assertFalse(residual_category('作品叫ポケモン。', preserved_terms=['ポケモン']))
        self.assertFalse(residual_category('这是中文正文。'))

    def test_cjk_conversion_does_not_change_paths_or_anchors(self):
        raw = '<html><body id="圖書"><a href="圖書.xhtml#圖書" title="a > b">軟體與圖書</a>' \
              '<a href="︵章︶.xhtml#︵節︶">︵文字︶</a>' \
              '<script>var name="圖書";</script><svg viewBox="0 0 1 1"><text>圖書</text></svg></body></html>'
        out = CjkNormalizer(output_mode='simplified').process(raw.encode(), 9).decode()
        self.assertIn('id="圖書"', out); self.assertIn('href="圖書.xhtml#圖書"', out)
        self.assertIn('title="a > b"', out); self.assertIn('软件与图书', out)
        self.assertIn('href="︵章︶.xhtml#︵節︶"', out)
        self.assertIn('>（文字）</a>', out)
        self.assertIn('var name="圖書"', out); self.assertIn('viewBox=', out)

    def test_body_root_anchor_head_and_direct_text_survive(self):
        fixture(self.source)
        book = EpubUnpacker(self.source).load_book(); self.assertIsNotNone(book)
        content = book.get_item_with_id('ch').get_content()
        root = etree.fromstring(content)
        self.assertEqual(root.get('id'), 'root-anchor')
        self.assertEqual(root.xpath('//*[local-name()="body"]')[0].get('id'), 'start')
        self.assertIn(b'Leading text.', content); self.assertTrue(root.xpath('//*[local-name()="style"]'))

    def test_no_ncx_book_packages_and_reopens(self):
        fixture(self.source)
        book = EpubUnpacker(self.source).load_book(); output = self.root / 'out.epub'
        self.assertTrue(EpubPackager(book, output).save())
        self.assertTrue(epub.read_epub(output).get_item_with_id('ncx'))
        self.assertEqual(audit_translated_epub_output(output)['navigation_broken_targets'], 0)

    def test_parent_relative_text_html_resources_are_recognized_and_safe(self):
        fixture(self.source, outside=True, html_type='text/html')
        book = EpubUnpacker(self.source).load_book(); self.assertIsNotNone(book)
        self.assertIsInstance(book.get_item_with_id('ch'), epub.EpubHtml)
        self.assertTrue(all(not item.get_name().startswith('../') for item in book.get_items()))
        self.assertEqual(book.toc[0].href, 'chapter.xhtml#start')
        output = self.root / 'out.epub'; self.assertTrue(EpubPackager(book, output).save())
        self.assertIsNotNone(epub.read_epub(output))
        self.assertEqual(audit_translated_epub_output(output)['navigation_broken_targets'], 0)
        manifest = build_manifest(str(self.source), 'fixture')
        self.assertEqual(sum(len(ch['chunks']) for ch in manifest['chapters'] if ch['chapter_kind'] == 'body'), 2)

    def test_valid_nav_wins_over_broken_legacy_ncx(self):
        fixture(self.source, ncx=True, bad_ncx=True)
        book = EpubUnpacker(self.source).load_book()
        self.assertEqual(book.toc[0].href, 'chapter.xhtml#start')

    def test_nested_ncx_targets_are_relative_to_ncx_not_opf(self):
        fixture(self.source, outside=True, ncx=True)
        book = EpubUnpacker(self.source).load_book(); output = self.root / 'out.epub'
        self.assertTrue(EpubPackager(book, output).save())
        audit = audit_translated_epub_output(output)
        self.assertEqual(audit['navigation_broken_targets'], 0)
        with zipfile.ZipFile(output) as archive:
            ncx = etree.fromstring(archive.read('EPUB/OEBPS/toc.ncx'))
            self.assertEqual(ncx.xpath('//*[local-name()="content"]/@src'), ['../chapter.xhtml#start'])

    def test_numeric_manifest_ids_upgrade_spine_without_renaming_anchors(self):
        fixture(self.source)
        with zipfile.ZipFile(self.source) as z:
            entries = [(i, z.read(i.filename).replace(b'id="ch"', b'id="123"').replace(b'idref="ch"', b'idref="123"')) for i in z.infolist()]
        with zipfile.ZipFile(self.source, 'w') as z:
            for info, raw in entries: z.writestr(info, raw)
        book = EpubUnpacker(self.source).load_book()
        self.assertEqual(book.spine[0][0], 'item-compat-0')
        self.assertIn(b'id="start"', book.get_item_with_id('item-compat-0').get_content())

    def test_page_list_auxiliary_target_is_non_linear_and_idempotent(self):
        fixture(self.source)
        book = EpubUnpacker(self.source).load_book()
        extra = epub.EpubHtml(uid='aux', file_name='extra.xhtml', title='附页',
            content='<html><body><p id="page">附页正文。</p></body></html>')
        book.add_item(extra)
        original_spine = list(book.spine)
        book.pages = [epub.Link('extra.xhtml#page', '1', 'p1'),
                      epub.Link('missing.xhtml#page', '2', 'p2'),
                      epub.Link('https://example.invalid/page', '3', 'p3')]
        EpubPackager._ensure_navigation_targets_in_spine(book)
        self.assertEqual(book.spine[:-1], original_spine)
        self.assertEqual(book.spine[-1], ('aux', 'no'))
        EpubPackager._ensure_navigation_targets_in_spine(book)
        self.assertEqual(len(book.spine), len(original_spine) + 1)
        self.assertEqual(extra.content, '<html><body><p id="page">附页正文。</p></body></html>')
        del book.pages[:]
        book.spine = original_spine
        extra.content = '<html><body><p epub-type="pagebreak" id="page">附页正文。</p></body></html>'
        EpubPackager._ensure_navigation_targets_in_spine(book)
        self.assertEqual(book.spine[-1], ('aux', 'no'))

    def test_percentage_image_dimensions_move_to_css_without_pixel_changes(self):
        fixture(self.source)
        book = EpubUnpacker(self.source).load_book(); item = book.get_item_with_id('ch')
        item.content = item.content.replace(b'</body>', b'<img src="cover.png" width="100%" height="50%"/></body>')
        root = etree.fromstring(item.get_content()); image = root.xpath('//*[local-name()="img"]')[0]
        self.assertNotIn('width', image.attrib); self.assertNotIn('height', image.attrib)
        self.assertIn('width:100%', image.get('style')); self.assertIn('height:50%', image.get('style'))

    def test_legacy_font_alignment_dimensions_and_empty_title_keep_content(self):
        root = etree.fromstring(b'<html><head><title/></head><body><blockquote id="anchor" align="center" width="90%" height="4"><font face="Serif" size="+1" color="black">Original text</font></blockquote></body></html>')
        before = ''.join(root.xpath('//*[local-name()="body"]')[0].itertext())
        upgrade_legacy_html(root, 'Fixture Book')
        self.assertEqual(root.find('head/title').text, 'Fixture Book')
        self.assertEqual(''.join(root.find('body').itertext()), before)
        block = root.find('body/blockquote'); self.assertEqual(block.get('id'), 'anchor')
        self.assertFalse(root.xpath('//font')); self.assertIn('text-align:center', block.get('style'))
        self.assertEqual(block.get('data-legacy-width'), '90%')
        self.assertEqual(block.get('data-legacy-height'), '4')
        self.assertNotIn('width:', block.get('style')); self.assertNotIn('height:', block.get('style'))
        root = etree.fromstring(b'<html><body><p width="0pt" height="9pt">Text</p></body></html>')
        upgrade_legacy_html(root)
        paragraph = root.find('body/p')
        self.assertNotIn('width', paragraph.attrib); self.assertNotIn('height', paragraph.attrib)
        self.assertEqual(paragraph.get('data-legacy-width'), '0pt')
        self.assertEqual(paragraph.get('data-legacy-height'), '9pt')
        self.assertFalse(paragraph.get('style'))
        root = etree.fromstring(b'<html><body><table width="90%" height="9pt"><tr><td>Text</td></tr></table></body></html>')
        upgrade_legacy_html(root)
        table = root.find('body/table')
        self.assertIn('width:90%', table.get('style')); self.assertIn('height:9pt', table.get('style'))

    def test_missing_body_is_not_fabricated(self):
        fixture(self.source, missing_body=True)
        unpacker = EpubUnpacker(self.source)
        self.assertIsNone(unpacker.load_book())
        self.assertIn('原书缺少正文', str(unpacker._last_error))

    def test_legacy_epub_type_keeps_note_semantics_and_existing_attributes(self):
        root = etree.fromstring(b'<html xmlns:epub="http://www.idpf.org/2007/ops"><body><aside id="note" epub-type="footnote">Text</aside><aside epub:type="endnote" epub-type="footnote">Other</aside></body></html>')
        upgrade_legacy_html(root)
        nodes = root.findall('body/aside')
        self.assertEqual(nodes[0].get('{http://www.idpf.org/2007/ops}type'), 'footnote')
        self.assertEqual(nodes[0].get('id'), 'note')
        self.assertEqual(nodes[1].get('{http://www.idpf.org/2007/ops}type'), 'endnote')
        self.assertEqual(nodes[1].get('data-legacy-epub-type'), 'footnote')
        self.assertFalse(root.xpath('//*[@epub-type]'))

    def test_absent_optional_page_map_does_not_block_text(self):
        fixture(self.source, missing_page_map=True)
        self.assertIsNotNone(EpubUnpacker(self.source).load_book())

    def test_legacy_metadata_upgrades_without_losing_values_or_prefixes(self):
        book = epub.EpubBook()
        normalize_metadata(book, b'<package xmlns="http://www.idpf.org/2007/opf" '
            b'prefix="schema: http://schema.org/"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
            b'xmlns:opf="http://www.idpf.org/2007/opf" xmlns:calibre="http://calibre.kovidgoyal.net/2009/metadata">'
            b'<dc:creator opf:role="aut" opf:file-as="Writer, A">A Writer</dc:creator>'
            b'<dc:identifier id="uid" opf:scheme="ISBN">123</dc:identifier>'
            b'<meta name="calibre:title_sort" content="A Book"/>'
            b'<dc:alternative>Other title</dc:alternative>'
            b'<meta property="schema:accessMode">textual</meta>'
            b'<meta property="hdf">legacy-value</meta>'
            b'<meta property="title-type">main</meta></metadata></package>')
        self.assertIn('schema: http://schema.org/', book.prefixes)
        creator, attrs = book.metadata[DC_NS]['creator'][0]
        self.assertEqual(creator, 'A Writer'); self.assertEqual(set(attrs), {'id'})
        metas = book.metadata[OPF_NS]['meta']
        self.assertTrue(any(value == 'aut' and attrs.get('property') == 'role' for value, attrs in metas))
        self.assertTrue(any(attrs.get('content') == 'A Book' for _, attrs in metas))
        self.assertTrue(any(attrs.get('content') == 'Other title' for _, attrs in metas))
        self.assertNotIn('alternative', book.metadata[DC_NS])
        self.assertTrue(any(attrs.get('name') == 'hdf' and attrs.get('content') == 'legacy-value' for _, attrs in metas))
        self.assertTrue(any(value == 'main' and attrs.get('property') == 'title-type' for value, attrs in metas))

    def test_valid_default_manifest_properties_are_not_removed(self):
        fixture(self.source)
        with zipfile.ZipFile(self.source) as z:
            entries = [(i, z.read(i.filename).replace(b'<item id="ch"', b'<item properties="glossary" id="ch"')) for i in z.infolist()]
        with zipfile.ZipFile(self.source, 'w') as z:
            for info, raw in entries: z.writestr(info, raw)
        book = EpubUnpacker(self.source).load_book()
        self.assertIn('glossary', book.get_item_with_id('ch').properties)

    def test_head_meta_language_and_repeat_serialization_are_stable(self):
        fixture(self.source)
        book = EpubUnpacker(self.source).load_book(); item = book.get_item_with_id('ch')
        item.content = item.content.replace(b'<html ', b'<html lang="en" xml:lang="zh" ').replace(
            b'</head>', b'<meta name="generator" value="fixture"/><link rel="stylesheet" href="style.css"/></head>')
        item.add_link(rel='stylesheet', href='style.css', type='text/css')
        first = item.get_content(); item.content = first; second = item.get_content()
        root = etree.fromstring(second)
        self.assertEqual(root.get('lang'), root.get('{http://www.w3.org/XML/1998/namespace}lang'))
        metas = root.xpath('//*[local-name()="meta"]')
        self.assertTrue(all('value' not in m.attrib for m in metas))
        self.assertEqual(len(root.xpath('//*[local-name()="style"]')), 1)
        self.assertEqual(len(root.xpath('//*[local-name()="link"]')), 1)

    def test_empty_and_duplicate_ncx_ids_are_repaired_only_in_navigation(self):
        fixture(self.source)
        book = EpubUnpacker(self.source).load_book()
        book.toc = [epub.Link('chapter.xhtml#start', 'One', ''), epub.Link('chapter.xhtml#start', 'Two', '')]
        output = self.root / 'out.epub'; self.assertTrue(EpubPackager(book, output).save())
        with zipfile.ZipFile(output) as archive:
            tree = etree.fromstring(archive.read('EPUB/toc.ncx'))
            ids = tree.xpath('//*[local-name()="navPoint"]/@id')
            self.assertEqual(len(ids), len(set(ids))); self.assertTrue(all(ids))
            body = etree.fromstring(archive.read('EPUB/chapter.xhtml'))
            self.assertEqual(body.xpath('//*[local-name()="body"]')[0].get('id'), 'start')

    def test_xhtml_raw_text_payloads_do_not_accumulate_entity_escapes(self):
        fixture(self.source)
        book = EpubUnpacker(self.source).load_book(); item = book.get_item_with_id('ch')
        item.content = item.content.replace(b'</head>',
            b'<script id="head-script">if (a &amp;&amp; b) { x = "a&amp;b"; }</script></head>').replace(
            b'</body>', b'<style id="body-style">p::after { content: "a&amp;b"; }</style>'
            b'<script id="body-script">if (a &lt; b &amp;&amp; c) { x = "a&amp;b"; }</script></body>')
        def payloads(raw):
            root = etree.fromstring(raw)
            return [(n.get('id'), n.text) for n in root.xpath('//*[local-name()="script" or local-name()="style"]')]
        expected = payloads(item.content)
        for _ in range(3):
            item.content = item.get_content()
            self.assertEqual(payloads(item.content), expected)
        first = self.root / 'first.epub'; self.assertTrue(EpubPackager(book, first).save())
        restored = EpubUnpacker(first).load_book()
        second = self.root / 'second.epub'; self.assertTrue(EpubPackager(restored, second).save())
        with zipfile.ZipFile(second) as archive:
            self.assertEqual(payloads(archive.read('EPUB/chapter.xhtml')), expected)

    def test_missing_font_source_preserves_present_local_and_remote_fallbacks(self):
        (self.root / 'good.ttf').write_bytes(b'font-fixture')
        css = '@font-face {font-family: A; src: url(missing.ttf) format("truetype"), local("Arial"), url(good.ttf), url(https://example.invalid/font.ttf);}'
        out, count = repair_font_sources(css, self.root / 'style.css', self.root)
        self.assertEqual(count, 1); self.assertNotIn('missing.ttf', out)
        self.assertIn('good.ttf', out); self.assertIn('local(', out); self.assertIn('https://', out)
        self.assertEqual((self.root / 'good.ttf').read_bytes(), b'font-fixture')
        self.assertEqual(repair_font_sources('p { color: red; }', self.root / 'style.css', self.root), ('p { color: red; }', 0))

    def test_kindle_processes_embedded_styles_but_keeps_hidden_content(self):
        raw = b'<html><head><style>.a{opacity:0.5}.hidden{opacity:0}</style></head><body><p style="opacity:0.25">Text</p><p style="opacity:0">Hidden</p></body></html>'
        out = DeviceProfileCompiler('kindle').process(raw, 9).decode()
        self.assertNotIn('opacity:0.5', out); self.assertNotIn('opacity:0.25', out)
        self.assertEqual(out.count('opacity:0'), 2)

    def test_locator_index_preserves_identity_numbering_without_sibling_rescans(self):
        raw = b'<html><body><div><p>Repeated.</p><span>x</span><p>Repeated.</p></div><p>Last.</p></body></html>'
        soup = BeautifulSoup(raw, 'html.parser')
        expected = [_build_locator(p, soup) for p in soup.find_all('p')]
        with patch('app.engine.chunk_extractor._xpath_segment', side_effect=AssertionError('quadratic sibling scan')):
            chunks = extract_chunks(raw, 'ch')
            large = extract_chunks(('<html><body>' + '<p>Repeated.</p>' * 2000 + '</body></html>').encode(), 'large')
        self.assertEqual([c.locator for c in chunks], expected)
        self.assertEqual([c.chunk_id for c in chunks], ['ch_0001', 'ch_0002', 'ch_0003'])
        self.assertEqual(large[-1].locator, '/html[1]/body[1]/p[2000]')
        self.assertEqual(len(set(c.locator for c in large)), 2000)


if __name__ == '__main__': unittest.main()
