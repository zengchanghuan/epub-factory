"""Tolerant local-resource repair, without credentials or model calls."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from lxml import etree

from app.engine.epub_resource_repair import (
    build_resource_repair_plan, ResourceRepairError, WARNING_ALIASES,
    WARNING_DOCUMENTS, WARNING_IMAGES, WARNING_MANIFEST, WARNING_NAVIGATION,
    WARNING_STYLES, MISSING_DOCUMENT_ATTRIBUTE,
)
from app.engine.unpacker import EpubUnpacker
from app.engine.packager import EpubPackager
from app.engine.epub_validation import validate_epub
from test_epub_fixture import minimal_epub_bytes


def xml_page(body):
    return ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Fixture</title></head><body>'
            + body + '</body></html>').encode()


class ResourceRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with zipfile.ZipFile(io.BytesIO(minimal_epub_bytes())) as archive:
            self.members = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        container = etree.fromstring(self.members['META-INF/container.xml'])
        self.opf = container.find('.//{*}rootfile').get('full-path')
        self.package = etree.fromstring(self.members[self.opf])
        self.base = str(Path(self.opf).parent)
        self.manifest = self.package.find('{*}manifest')
        self.spine = self.package.find('{*}spine')
        self.body_item = next(item for item in self.manifest if item.get('id') == self.spine[0].get('idref'))
        self.body = self.base + '/' + self.body_item.get('href')
        self.nav_item = next(item for item in self.manifest if 'nav' in item.get('properties', '').split())
        self.nav = self.base + '/' + self.nav_item.get('href')

    def add(self, uid, name, kind, raw=None, spine=False):
        item = etree.SubElement(self.manifest, '{http://www.idpf.org/2007/opf}item',
                               id=uid, href=name, attrib={'media-type': kind})
        if raw is not None: self.members[self.base + '/' + name] = raw
        if spine: etree.SubElement(self.spine, '{http://www.idpf.org/2007/opf}itemref', idref=uid)
        return item

    def write(self):
        self.members[self.opf] = etree.tostring(self.package)
        path = self.root / 'source.epub'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, raw in self.members.items(): archive.writestr(name, raw)
        return path

    def plan(self):
        with zipfile.ZipFile(self.write()) as archive:
            return build_resource_repair_plan(archive, self.opf)

    def test_valid_file_unchanged_without_warnings(self):
        plan = self.plan()
        self.assertEqual(plan.replacements, {})
        self.assertEqual(plan.warnings, [])
        self.assertEqual(plan.readable_body_count, 1)

    def test_unique_nav_extension_alias_recovers_original_labels_and_links(self):
        original = self.members.pop(self.nav)
        actual = self.nav.rsplit('.', 1)[0] + '.html'
        body_href = self.body_item.get('href')
        self.members[actual] = original.replace(body_href.encode(), (body_href.rsplit('.', 1)[0] + '.html').encode())
        plan = self.plan()
        self.assertIn(WARNING_ALIASES, plan.warnings)
        self.assertNotIn(WARNING_DOCUMENTS, plan.warnings)
        self.assertIn(body_href.encode(), plan.replacements[actual])
        self.assertIn(actual.split('/')[-1].encode(), plan.replacements[self.opf])
        self.assertNotIn(self.nav, plan.replacements)

    def test_missing_image_has_explicit_svg_and_original_text_preserved(self):
        self.members[self.body] = xml_page('<p>Original readable body.</p><img src="missing.png" alt="Cover"/>')
        before = hashlib.sha256(self.write().read_bytes()).hexdigest()
        plan = self.plan()
        self.assertIn(WARNING_IMAGES, plan.warnings)
        self.assertIn(b'Original readable body.', plan.replacements[self.body])
        image = next(name for name in plan.replacements if name.endswith('.svg'))
        self.assertIn('原文件缺少此图片'.encode(), plan.replacements[image])
        self.assertEqual(before, hashlib.sha256(self.write().read_bytes()).hexdigest())
        loaded = EpubUnpacker(self.write()); self.assertIsNotNone(loaded.load_book())
        self.assertIn(WARNING_IMAGES, loaded.source_warnings)

    def test_missing_declared_image_reuses_manifest_id(self):
        self.add('cover-image', 'missing.png', 'image/png').set('properties', 'cover-image')
        plan = self.plan()
        repaired = etree.fromstring(plan.replacements[self.opf])
        item = repaired.find('.//{*}item[@id="cover-image"]')
        self.assertEqual(item.get('media-type'), 'image/svg+xml')
        self.assertEqual(item.get('properties'), 'cover-image')

    def test_existing_undeclared_image_and_css_are_preserved(self):
        self.members[self.body] = xml_page('<p>Text</p><img src="extra.svg"/>').replace(b'</head>', b'<link rel="stylesheet" href="extra.css"/></head>')
        self.members[self.base + '/extra.svg'] = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
        self.members[self.base + '/extra.css'] = b'p { color: navy; }'
        plan = self.plan()
        self.assertIn(WARNING_MANIFEST, plan.warnings)
        self.assertNotIn(self.base + '/extra.svg', plan.replacements)
        self.assertNotIn(self.base + '/extra.css', plan.replacements)
        self.assertIn(b'extra.svg', plan.replacements[self.opf])
        self.assertIn(b'extra.css', plan.replacements[self.opf])

    def test_missing_stylesheet_and_nested_import_fall_back(self):
        self.add('style', 'style.css', 'text/css', b'@import "missing.css"; p { background: url(missing.png); }')
        plan = self.plan()
        self.assertIn(WARNING_STYLES, plan.warnings)
        self.assertIn(WARNING_IMAGES, plan.warnings)
        self.assertIn(self.base + '/missing.css', plan.replacements)
        self.assertIn(b'url(epub-factory-missing-image-', plan.replacements[self.base + '/style.css'])

    def test_css_fonts_and_external_links_are_not_fetched(self):
        css = b'@font-face { font-family: X; src:url(missing.ttf); } p { background:url(https://example.invalid/a.png); }'
        self.add('style', 'style.css', 'text/css', css)
        with patch('socket.socket.connect', side_effect=AssertionError('No network allowed')):
            plan = self.plan()
        self.assertNotIn(self.base + '/style.css', plan.replacements)
        self.assertEqual(plan.warnings, [])

    def test_css_comments_do_not_create_missing_image_warnings(self):
        raw = b'/* url(missing.png); @import "missing.css"; */ @font-face { /* note */ src:url(missing.ttf); }'
        self.add('style', 'style.css', 'text/css', raw)
        plan = self.plan()
        self.assertEqual(plan.warnings, [])
        self.assertEqual(plan.replacements, {})

    def test_missing_ncx_rebuild_loads_and_preserves_body(self):
        item = self.add('ncx', 'toc.ncx', 'application/x-dtbncx+xml')
        self.spine.set('toc', item.get('id'))
        plan = self.plan()
        self.assertIn(WARNING_NAVIGATION, plan.warnings)
        unpacker = EpubUnpacker(self.write())
        self.assertIsNotNone(unpacker.load_book(), unpacker._last_error)

    def test_css_import_cycles_terminate_and_preserve_bytes(self):
        self.add('style', 'style.css', 'text/css', b'@import "other.css";')
        self.add('other', 'other.css', 'text/css', b'@import "style.css";')
        self.assertEqual(self.plan().replacements, {})

    def test_missing_body_notice_preserves_links_and_is_not_fake_translation(self):
        self.add('lost', 'lost.xhtml', 'application/xhtml+xml', spine=True)
        self.members[self.body] = xml_page('<p>Still present.</p><a href="lost.xhtml#missing-anchor">Next</a>')
        plan = self.plan()
        notice = plan.replacements[self.base + '/lost.xhtml']
        tree = etree.fromstring(notice)
        self.assertEqual(tree.find('{*}body').get(MISSING_DOCUMENT_ATTRIBUTE), 'true')
        self.assertIn(b'id="missing-anchor"', notice)
        self.assertIn('原文件缺少本章节'.encode(), notice)
        self.assertIn(WARNING_DOCUMENTS, plan.warnings)
        self.assertEqual(plan.readable_body_count, 1)

    def test_existing_svg_spine_counts_as_readable_content(self):
        self.body_item.set('media-type', 'image/svg+xml')
        self.body_item.set('href', 'drawing.svg')
        del self.members[self.body]
        self.members[self.base + '/drawing.svg'] = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100"/><text x="1" y="20">Original SVG text</text></svg>'
        self.members[self.nav] = self.members[self.nav].replace(b'chapter.xhtml', b'drawing.svg')
        self.assertEqual(self.plan().readable_body_count, 1)

    def test_all_body_missing_is_fatal_even_with_nav(self):
        del self.members[self.body]
        with self.assertRaisesRegex(ResourceRepairError, '没有可读取'):
            self.plan()

    def test_only_existing_notice_does_not_count_as_real_body(self):
        self.members[self.body] = xml_page('<h1>原文件缺少本章节</h1><p>本页仅说明原文件缺失，不代表译文；其余可用章节继续处理。</p>').replace(b'<body>', b'<body data-epub-factory-missing-document="true">')
        with self.assertRaisesRegex(ResourceRepairError, '没有可读取'):
            self.plan()

    def test_missing_nav_is_rebuilt_from_available_reading_order(self):
        del self.members[self.nav]
        plan = self.plan()
        self.assertIn(WARNING_NAVIGATION, plan.warnings)
        self.assertIn(self.body_item.get('href').encode(), plan.replacements[self.nav])
        unpacker = EpubUnpacker(self.write())
        self.assertIsNotNone(unpacker.load_book(), unpacker._last_error)

    def test_undeclared_linked_chapter_is_added_without_losing_text(self):
        self.members[self.body] = xml_page('<p>Body.</p><a href="extra.xhtml">Extra</a>')
        extra = self.base + '/extra.xhtml'
        self.members[extra] = xml_page('<p>Additional original content.</p>')
        plan = self.plan()
        self.assertIn(WARNING_MANIFEST, plan.warnings)
        self.assertIn(b'extra.xhtml', plan.replacements[self.opf])
        self.assertNotIn(extra, plan.replacements)

    def test_html_fallback_does_not_duplicate_namespace(self):
        self.members[self.body] = xml_page('<p>Text&nbsp;with entity.</p>')
        unpacker = EpubUnpacker(self.write()); book = unpacker.load_book()
        self.assertIsNotNone(book, unpacker._last_error)
        chapter = book.get_item_with_id(self.body_item.get('id'))
        raw = chapter.get_content()
        etree.fromstring(raw)
        self.assertEqual(raw.count(b'xmlns="http://www.w3.org/1999/xhtml"'), 1)

    def test_marker_alone_does_not_hide_present_body(self):
        self.members[self.body] = xml_page('<p>Actual original prose.</p>').replace(b'<body>', b'<body data-epub-factory-missing-document="true">')
        self.assertEqual(self.plan().readable_body_count, 1)

    def test_unknown_missing_resource_remains_fatal(self):
        self.add('audio', 'missing.mp3', 'audio/mpeg')
        with self.assertRaisesRegex(ResourceRepairError, '无法安全恢复'):
            self.plan()

    def test_unsafe_paths_are_not_repaired(self):
        self.members[self.body] = xml_page('<p>Body</p><img src="../../outside.png"/>')
        with self.assertRaisesRegex(ResourceRepairError, '路径不安全'):
            self.plan()

    def test_duplicate_zip_members_are_not_repaired(self):
        path = self.write()
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr(self.body, b'bad')
        with zipfile.ZipFile(path) as archive, self.assertRaises(ResourceRepairError):
            build_resource_repair_plan(archive, self.opf)

    def test_warnings_survive_packaging_and_reload(self):
        self.members[self.body] = xml_page('<p>Original body.</p><img src="missing.png"/>')
        unpacker = EpubUnpacker(self.write()); book = unpacker.load_book()
        out = self.root / 'out.epub'
        self.assertTrue(EpubPackager(book, out).save())
        reloaded = EpubUnpacker(out); self.assertIsNotNone(reloaded.load_book())
        self.assertIn(WARNING_IMAGES, reloaded.source_warnings)

    def test_notices_and_repaired_resources_pass_real_epubcheck(self):
        jar = Path(os.environ.get('EPUBCHECK_JAR', '/tmp/epub-validator/epubcheck-5.1.0/epubcheck.jar'))
        if not jar.exists(): self.skipTest('EPUBCheck not available')
        self.add('lost', 'lost.xhtml', 'application/xhtml+xml', spine=True)
        self.members[self.body] = xml_page('<p>Original body.</p><img src="missing.png" alt="Missing"/><a href="lost.xhtml#anchor">Next</a>')
        del self.members[self.nav]
        unpacker = EpubUnpacker(self.write()); book = unpacker.load_book()
        self.assertIsNotNone(book, unpacker._last_error)
        out = self.root / 'out.epub'; self.assertTrue(EpubPackager(book, out).save())
        result = validate_epub(out, jar)
        self.assertTrue(result.passed, result)


if __name__ == '__main__':
    unittest.main()
