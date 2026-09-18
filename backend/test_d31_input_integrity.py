"""No-model input gate: unreadable books fail; repairable local issues continue."""
import io
import logging
import os
import shutil
import tempfile
import unittest
import uuid
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.domain import epub_input_integrity as integrity
from app.engine.unpacker import EpubUnpacker


def fixture(*, missing=(), extra_items='', body='', extras=None, outside=False,
            body_type='application/xhtml+xml', numeric_id=False, ncx=False, nav=True):
    """Private-text-free EPUB with independently editable declarations/files."""
    prefix = '../' if outside else ''
    uid = '123' if numeric_id else 'body'
    items = f'<item id="{uid}" href="{prefix}body.xhtml" media-type="{body_type}"/>'
    if nav:
        items += f'<item id="nav" href="{prefix}nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    if ncx:
        items += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    content = {
        'mimetype': b'application/epub+zip',
        'META-INF/container.xml': b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        'OEBPS/book.opf': (f'<package xmlns="http://www.idpf.org/2007/opf"><metadata/>'
                          f'<manifest>{items}{extra_items}</manifest><spine toc="ncx"><itemref idref="{uid}"/></spine></package>').encode(),
        ('' if outside else 'OEBPS/') + 'body.xhtml': ('<html><body><p>Fixture text.</p>' + body + '</body></html>').encode(),
    }
    if nav:
        content[('' if outside else 'OEBPS/') + 'nav.xhtml'] = b'<html><body><nav epub:type="toc"><ol><li><a href="body.xhtml#chapter">Chapter</a></li></ol></nav></body></html>'
    if ncx:
        content['OEBPS/toc.ncx'] = b'<ncx><navMap><navPoint><content src="missing.xhtml"/></navPoint></navMap></ncx>'
    content.update(extras or {})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, raw in content.items():
            if name not in missing:
                archive.writestr(name, raw)
    return stream.getvalue()


def replace_member(data, name, transform):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, 'w') as dest:
        for info in source.infolist():
            raw = source.read(info.filename)
            dest.writestr(info, transform(raw) if info.filename == name else raw)
    return output.getvalue()


class ResourceTests(unittest.TestCase):
    def check(self, data):
        return integrity.validate_epub_resources(io.BytesIO(data))

    def test_no_readable_body_is_rejected_but_missing_nav_is_repairable(self):
        with self.assertRaises(integrity.EpubInputError):
            self.check(fixture(missing=('OEBPS/body.xhtml',)))
        self.assertTrue(self.check(fixture(missing=('OEBPS/nav.xhtml',))))

    def test_declared_missing_cover_is_a_warning(self):
        warnings = self.check(fixture(extra_items='<item id="cover" href="cover.jpg" media-type="image/jpeg"/>'))
        self.assertTrue(warnings)

    def test_one_missing_chapter_does_not_block_other_existing_chapters(self):
        data = fixture(extra_items='<item id="missing" href="missing.xhtml" media-type="application/xhtml+xml"/>')
        data = replace_member(data, 'OEBPS/book.opf', lambda raw: raw.replace(
            b'</spine>', b'<itemref idref="missing"/></spine>'))
        source = io.BytesIO(data)
        warnings = integrity.validate_epub_resources(source)
        self.assertTrue(any('缺少部分章节' in warning for warning in warnings))
        self.assertEqual(source.getvalue(), data)

    def test_referenced_images_and_css_images_are_checked(self):
        for body in ('<img src="lost.png"/>', '<svg><image xlink:href="lost.svg#image"/></svg>',
                     '<p style="background-image:url(lost.png)">X</p>',
                     '<style>p { background: url("lost.png") }</style>',
                     '<picture><source srcset="lost.png 2x"/></picture>',
                     '<img srcset="data:image/png;base64,YQ== 1x, lost.png 2x"/>'):
            with self.subTest(body=body):
                self.assertTrue(self.check(fixture(body=body)))
        self.assertTrue(self.check(fixture(extra_items='<item id="css" href="style.css" media-type="text/css"/>',
                               extras={'OEBPS/style.css': b'p { background:url(lost.png) }'})))

    def test_relative_legacy_type_and_numeric_ids_remain_supported(self):
        self.check(fixture(outside=True, body_type='text/html', numeric_id=True))
        self.check(fixture(body='<img src="../images/a%20b.png?rev=2#view"/>',
                           extra_items='<item id="picture" href="../images/a%20b.png" media-type="image/png"/>',
                           extras={'images/a b.png': b'\x89PNG fixture'}))

    def test_optional_fonts_and_page_map_do_not_block(self):
        self.check(fixture(extra_items='<item id="font" href="missing.otf" media-type="font/otf"/>'
                           '<item id="map" href="missing.xml" media-type="application/oebps-page-map+xml"/>'
                           '<item id="css" href="style.css" media-type="text/css"/>',
                           extras={'OEBPS/style.css': b'@font-face { font-family: test; src: url(missing.otf) } p {font-family:test,serif}'}))

    def test_missing_css_is_repairable_but_unsupported_dependencies_still_fail(self):
        self.assertTrue(self.check(fixture(extra_items='<item id="css" href="style.css" media-type="text/css"/>')))
        for kind, filename in (('application/smil+xml', 'audio.smil'),
                               ('application/octet-stream', 'unknown.bin')):
            data = fixture(extra_items=f'<item id="required" href="{filename}" media-type="{kind}"/>')
            with self.subTest(kind=kind), self.assertRaisesRegex(integrity.EpubInputError, '无法安全恢复'):
                self.check(data)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'source.epub'; path.write_bytes(data)
                self.assertIsNone(EpubUnpacker(path).load_book())

    def test_missing_dependent_font_rejected_by_gate_and_actual_reader(self):
        base = fixture(extra_items='<item id="font" href="missing.otf" media-type="font/otf"/>')
        variants = [
            lambda raw: raw.replace(b'idref="body"', b'idref="font"'),
            lambda raw: raw.replace(b'id="body"', b'id="body" fallback="font"'),
            lambda raw: raw.replace(b'id="body"', b'id="body" media-overlay="font"'),
            lambda raw: raw.replace(b'<metadata/>', b'<metadata><meta refines="#font" property="x:test">x</meta></metadata>'),
        ]
        for transform in variants:
            data = replace_member(base, 'OEBPS/book.opf', transform)
            with self.subTest(transform=transform), self.assertRaisesRegex(integrity.EpubInputError, '无法安全恢复'):
                self.check(data)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'source.epub'; path.write_bytes(data)
                self.assertIsNone(EpubUnpacker(path).load_book())

    def test_optional_font_removal_actually_loads_and_preserves_present_fonts(self):
        from test_epub_fixture import minimal_epub_bytes
        data = replace_member(minimal_epub_bytes(), 'EPUB/package.opf', lambda raw: raw.replace(
            b'</manifest>', b'<item id="font" href="missing.otf" media-type="font/otf"/>'
            b'<item id="present-font" href="present.otf" media-type="font/otf"/></manifest>'))
        stream = io.BytesIO(data)
        with zipfile.ZipFile(stream, 'a') as archive:
            archive.writestr('EPUB/present.otf', b'present-font-bytes')
        data = stream.getvalue()
        self.check(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.epub'; path.write_bytes(data)
            book = EpubUnpacker(path).load_book()
            self.assertIsNotNone(book)
            self.assertIsNone(book.get_item_with_id('font'))
            self.assertEqual(book.get_item_with_id('present-font').get_content(), b'present-font-bytes')
            self.assertEqual(path.read_bytes(), data)

    def test_optional_missing_font_full_conversion_passes_real_epubcheck(self):
        # Integration layer is run when EPUBCheck is provisioned, including CI.
        with patch('dotenv.load_dotenv', return_value=False):
            from app.engine.compiler import EPUBCHECK_JAR
            from app.converter import EpubConverter
            from app.models import OutputMode
        if not Path(EPUBCHECK_JAR).is_file() or not shutil.which('java'):
            self.skipTest('requires the real EPUBCheck JAR and Java runtime')
        from test_epub_fixture import minimal_epub_bytes
        data = replace_member(minimal_epub_bytes(), 'EPUB/package.opf', lambda raw: raw.replace(
            b'</manifest>', b'<item id="font" href="missing.otf" media-type="font/otf"/>'
            b'<item id="css" href="font.css" media-type="text/css"/></manifest>'))
        data = replace_member(data, 'EPUB/chapter.xhtml', lambda raw: raw.replace(
            b'</head>', b'<link href="font.css" rel="stylesheet" type="text/css"/></head>'))
        stream = io.BytesIO(data)
        with zipfile.ZipFile(stream, 'a') as archive:
            archive.writestr('EPUB/font.css', '@font-face {font-family: Book; src:url(missing.otf)} p{font-family:Book,serif}')
        data = stream.getvalue()
        self.check(data)
        with tempfile.TemporaryDirectory() as directory, patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            source, output = Path(directory) / 'source.epub', Path(directory) / 'output.epub'
            source.write_bytes(data)
            self.assertIsNotNone(EpubUnpacker(source).load_book())
            with patch('app.engine.cleaners.semantics_translator.SemanticsTranslator.__init__', side_effect=AssertionError('model forbidden')):
                result = EpubConverter().convert_file_to_horizontal(source, output, OutputMode.simplified, enable_translation=False)
            self.assertTrue(result.validation_passed, result.message)
            self.assertEqual(source.read_bytes(), data)
            with zipfile.ZipFile(output) as archive:
                styles = b'\n'.join(archive.read(name) for name in archive.namelist() if name.endswith('.css'))
                self.assertNotIn(b'missing.otf', styles)
                self.assertIn(b'serif', styles)

    def test_last_valid_media_type_rootfile_matches_actual_reader(self):
        from test_epub_fixture import minimal_epub_bytes
        from app.engine.epub_compat import package_path
        base = minimal_epub_bytes()
        with zipfile.ZipFile(io.BytesIO(base)) as archive:
            alternate = archive.read('EPUB/package.opf').replace(b'href="chapter.xhtml"', b'href="lost.xhtml"')
        stream = io.BytesIO(base)
        with zipfile.ZipFile(stream, 'a') as archive:
            archive.writestr('EPUB/alternate.opf', alternate)
        for last, good in (('alternate.opf', False), ('package.opf', True)):
            first = 'package.opf' if last == 'alternate.opf' else 'alternate.opf'
            container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                         f'<rootfile full-path="EPUB/{first}" media-type="application/oebps-package+xml"/>'
                         f'<rootfile full-path="EPUB/{last}" media-type="application/oebps-package+xml"/>'
                         '<rootfile full-path="ignored.xml" media-type="application/not-opf"/>'
                         '</rootfiles></container>').encode()
            data = replace_member(stream.getvalue(), 'META-INF/container.xml', lambda _: container)
            with self.subTest(last=last), tempfile.TemporaryDirectory() as directory:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    self.assertEqual(package_path(archive), 'EPUB/' + last)
                source = Path(directory) / 'source.epub'; source.write_bytes(data)
                if good:
                    self.check(data)
                    self.assertIsNotNone(EpubUnpacker(source).load_book())
                else:
                    with self.assertRaisesRegex(integrity.EpubInputError, '可读取的正文'):
                        self.check(data)
                    self.assertIsNone(EpubUnpacker(source).load_book())

    def test_rootfile_without_opf_media_type_is_rejected(self):
        data = replace_member(fixture(), 'META-INF/container.xml', lambda raw: raw.replace(b' media-type="application/oebps-package+xml"', b''))
        with self.assertRaises(integrity.EpubInputError):
            self.check(data)

    def test_missing_stylesheet_links_and_imports_are_repaired(self):
        self.assertTrue(self.check(fixture(body='<link rel="stylesheet" href="lost.css"/>')))
        for directive in ('@import url(lost.css);', '@import "lost.css";', "@import 'lost.css' screen;", '@import url("lost.css") print;'):
            with self.subTest(directive=directive):
                self.assertTrue(self.check(fixture(extra_items='<item id="css" href="style.css" media-type="text/css"/>',
                                   extras={'OEBPS/style.css': directive.encode()})))

    def test_existing_undeclared_stylesheet_and_image_are_preserved(self):
        for css, message in ((b'p{background:url(lost.png)}', '图片'), (b'p{margin:1em}', '未完整声明')):
            with self.subTest(css=css):
                self.assertTrue(self.check(fixture(body='<link href="extra.css" rel="stylesheet"/>', extras={'OEBPS/extra.css': css})))
        self.assertTrue(self.check(fixture(body='<img src="present.svg"/>', extras={'OEBPS/present.svg': b'<svg xmlns="http://www.w3.org/2000/svg"/>'})))

    def test_stylesheet_import_cycles_have_bounded_plan_and_validation_reads(self):
        data = fixture(body='<link rel="stylesheet" href="a.css"/>',
            extra_items='<item id="a" href="a.css" media-type="text/css"/><item id="b" href="b.css" media-type="text/css"/>',
            extras={'OEBPS/a.css': b'@import "b.css"; p{margin:1em}', 'OEBPS/b.css': b'@import url(a.css);'})
        opened = []
        original = zipfile.ZipFile.open
        def tracked(archive, name, *args, **kwargs):
            opened.append(name.filename if hasattr(name, 'filename') else name)
            return original(archive, name, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', tracked):
            self.check(data)
        self.assertEqual(opened.count('OEBPS/a.css'), 2)  # Plan + independent strict check.
        self.assertEqual(opened.count('OEBPS/b.css'), 2)

    def test_undeclared_css_and_image_survive_actual_packager(self):
        with patch('dotenv.load_dotenv', return_value=False):
            from app.engine.compiler import EPUBCHECK_JAR
            from app.converter import EpubConverter
            from app.models import OutputMode
        if not Path(EPUBCHECK_JAR).is_file() or not shutil.which('java'):
            self.skipTest('requires the real EPUBCheck JAR and Java runtime')
        from test_epub_fixture import minimal_epub_bytes
        data = replace_member(minimal_epub_bytes(), 'EPUB/chapter.xhtml', lambda raw: raw.replace(
            b'</head>', b'<link href="extra.css" rel="stylesheet" type="text/css"/></head>').replace(
            b'</body>', b'<img src="picture.svg" alt="fixture"/></body>'))
        stream = io.BytesIO(data)
        with zipfile.ZipFile(stream, 'a') as archive:
            archive.writestr('EPUB/extra.css', 'p{margin:1em}')
            archive.writestr('EPUB/picture.svg', '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>')
        data = stream.getvalue()
        self.assertTrue(self.check(data))
        with tempfile.TemporaryDirectory() as directory, patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            source, output = Path(directory) / 'source.epub', Path(directory) / 'output.epub'
            source.write_bytes(data)
            self.assertIsNotNone(EpubUnpacker(source).load_book())
            result = EpubConverter().convert_file_to_horizontal(source, output, OutputMode.simplified, enable_translation=False)
            self.assertTrue(result.validation_passed, result.message)
            with zipfile.ZipFile(output) as archive:
                self.assertTrue(any(name.endswith('extra.css') for name in archive.namelist()))
                self.assertTrue(any(name.endswith('picture.svg') for name in archive.namelist()))

    def test_missing_nav_ncx_and_dead_toc_targets_are_recoverable(self):
        self.check(fixture(ncx=True))
        self.assertTrue(self.check(fixture(ncx=True, missing=('OEBPS/toc.ncx',))))
        self.assertTrue(self.check(fixture(ncx=True, nav=False)))
        self.assertTrue(self.check(fixture(extras={'OEBPS/nav.xhtml': b'<nav epub:type="toc"><ol><li><a href="lost.xhtml">Lost</a></li></ol></nav>'})))

    def test_body_nav_does_not_override_required_ncx(self):
        self.check(fixture(ncx=True, nav=False, body='<nav epub:type="toc"><ol><li><a href="body.xhtml">Chapter</a></li></ol></nav>'))

    def test_external_and_embedded_images_are_not_fetched(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network access')):
            self.check(fixture(body='<img src="https://example.invalid/cover.png"/><img src="data:image/png;base64,YQ=="/>'))

    def test_external_epub2_doctype_is_not_fetched(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network access')):
            self.check(fixture(extras={'META-INF/container.xml': b'<!DOCTYPE container SYSTEM "https://example.invalid/epub.dtd"><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'}))

    def test_bad_zip_xml_entities_and_path_escape_have_safe_messages(self):
        cases = [b'PK broken private book text', fixture(body='<img src="../../secret.png"/>'),
                 fixture(extras={'META-INF/container.xml': b'<container>private book text<'}),
                 fixture(extras={'META-INF/container.xml': b'<!DOCTYPE container [<!ENTITY e SYSTEM "file:///private/sentinel">]><container><rootfiles><rootfile full-path="&e;"/></rootfiles></container>'})]
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(integrity.EpubInputError) as error:
                self.check(data)
            self.assertNotIn('private', str(error.exception))
            self.assertNotIn('sentinel', str(error.exception))

    def test_duplicate_zip_entries_are_rejected(self):
        data = io.BytesIO(fixture())
        with zipfile.ZipFile(data, 'a') as archive:
            with self.assertWarns(UserWarning):
                archive.writestr('OEBPS/body.xhtml', b'duplicate')
        with self.assertRaises(integrity.EpubInputError):
            self.check(data.getvalue())

    def test_zip_and_xml_size_limits_are_enforced_before_parse(self):
        for limit in ('MAX_UNCOMPRESSED_BYTES', 'MAX_ENTRIES'):
            with patch.object(integrity, limit, 1), self.assertRaisesRegex(integrity.EpubInputError, '过大'):
                self.check(fixture())
        # Metadata has a separate bound so an OPF cannot consume a book's budget.
        with patch.object(integrity, 'MAX_METADATA_BYTES', 1), self.assertRaisesRegex(integrity.EpubInputError, '过大'):
            self.check(fixture())

    def test_stream_position_is_restored_on_success_and_error(self):
        for data, good in ((fixture(), True), (fixture(missing=('OEBPS/body.xhtml',)), False)):
            stream = io.BytesIO(data); stream.seek(3)
            if good:
                integrity.validate_epub_resources(stream)
            else:
                with self.assertRaises(integrity.EpubInputError):
                    integrity.validate_epub_resources(stream)
            self.assertEqual(stream.tell(), 3)

    def test_document_read_count_is_bounded_after_repair_validation(self):
        opened = []
        data = fixture()
        original = zipfile.ZipFile.open
        def tracked(archive, name, *args, **kwargs):
            opened.append(name.filename if hasattr(name, 'filename') else name)
            return original(archive, name, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', tracked):
            self.check(data)
        self.assertLessEqual(opened.count('OEBPS/body.xhtml'), 2)
        self.assertLessEqual(opened.count('OEBPS/nav.xhtml'), 2)


class UploadGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = tempfile.TemporaryDirectory()
        cls.root = Path(cls.runtime.name)
        cls.setup = ExitStack()
        cls.setup.enter_context(patch.dict(os.environ, {
            'DATABASE_URL': 'sqlite:///' + str(cls.root / 'jobs.db'),
            'OPENAI_API_KEY': 'offline-only', 'SKIP_PAYMENT_CHECK': '0',
            'ALIPAY_APP_ID': '', 'DOWNLOAD_SIGN_SECRET': 'offline-only',
        }, clear=True))
        cls.setup.enter_context(patch('dotenv.load_dotenv', return_value=False))
        cls.setup.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        from fastapi.testclient import TestClient
        import app.main as main
        from app.storage_db import PersistentJobStore
        cls.main = main
        cls.setup.enter_context(patch.object(main, 'job_store', PersistentJobStore()))
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close(); cls.setup.close(); cls.runtime.cleanup()

    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.uploads = self.root / uuid.uuid4().hex; self.uploads.mkdir()
        self.stack.enter_context(patch.object(self.main, 'UPLOAD_DIR', self.uploads))
        self.add = self.stack.enter_context(patch.object(self.main.job_store, 'add', wraps=self.main.job_store.add))
        self.pay = self.stack.enter_context(patch.object(self.main, 'create_alipay_page_pay', return_value='https://offline.invalid/pay'))
        self.qr = self.stack.enter_context(patch('app.infra.alipay.create_alipay_precreate', return_value='alipay://offline'))
        self.enqueue = self.stack.enter_context(patch.object(self.main, '_enqueue_conversion'))
        self.batch = self.stack.enter_context(patch.object(self.main, '_enqueue_batch'))
        self.process = self.stack.enter_context(patch.object(self.main, 'process_job'))

    def assert_no_effects(self):
        for mock in (self.add, self.pay, self.qr, self.enqueue, self.batch, self.process):
            mock.assert_not_called()
        self.assertEqual(list(self.uploads.iterdir()), [])

    def test_all_single_routes_reject_before_file_order_payment_and_probe(self):
        bad_books = [fixture(missing=('OEBPS/body.xhtml',)), b'PK invalid archive']
        for data in bad_books:
            for path in ('/api/v1/jobs', '/api/v2/jobs'):
                for translation in ('false', 'true'):
                    with self.subTest(path=path, translation=translation), patch.object(self.main, 'build_translation_preflight') as probe:
                        response = self.client.post(path, files={'file': ('bad.epub', data)},
                            data={'enable_translation': translation, 'profile_confirmation': 'true'})
                        expected = 410 if path == '/api/v1/jobs' and translation == 'true' else 400
                        self.assertEqual(response.status_code, expected, response.text)
                        probe.assert_not_called(); self.assert_no_effects()

    def test_batch_valid_then_bad_stores_nothing_and_charges_nothing(self):
        response = self.client.post('/api/v2/batches', files=[
            ('files', ('good.epub', fixture())),
            ('files', ('bad.epub', fixture(missing=('OEBPS/body.xhtml',)))),
        ])
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_no_effects()

    def test_unsupported_missing_resources_never_create_payment(self):
        for kind, filename in (('application/octet-stream', 'unknown.bin'), ('application/smil+xml', 'audio.smil')):
            with self.subTest(kind=kind):
                data = fixture(extra_items=f'<item id="required" href="{filename}" media-type="{kind}"/>')
                response = self.client.post('/api/v2/jobs', files={'file': ('bad.epub', data)})
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_no_effects()

    def test_valid_epub_reaches_payment_and_non_epub_gate_is_unchanged(self):
        from fastapi import UploadFile
        response = self.client.post('/api/v2/jobs', files={'file': ('good.epub', fixture(outside=True))})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.qr.call_count + self.pay.call_count, 1)
        self.assertEqual(self.add.call_count, 1)
        self.main._validate_upload_format(UploadFile(filename='notes.md', file=io.BytesIO(b'# notes')))

    def test_repairable_input_reaches_checkout_with_durable_warning(self):
        data = fixture(body='<img src="lost.jpg"/>')
        response = self.client.post('/api/v2/jobs', files={'file': ('partial.epub', data)})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['source_warnings'])
        self.assertEqual(self.qr.call_count + self.pay.call_count, 1)
        job = self.main.job_store.get(body['job_id'])
        self.assertEqual(job.translation_stats['source_warnings'], body['source_warnings'])
        self.assertEqual(Path(job.input_path).read_bytes(), data)
        detail = self.client.get('/api/v2/jobs/' + job.id, headers={'X-Job-Token': body['access_token']})
        self.assertEqual(detail.json()['source_warnings'], body['source_warnings'])

    def test_repairable_batch_continues_and_reports_warnings_per_book(self):
        response = self.client.post('/api/v2/batches', files=[
            ('files', ('normal.epub', fixture())),
            ('files', ('partial.epub', fixture(body='<img src="lost.png"/>'))),
        ])
        self.assertEqual(response.status_code, 200, response.text)
        jobs = response.json()['jobs']
        self.assertEqual(len(jobs), 2)
        self.assertFalse(jobs[0]['source_warnings'])
        self.assertTrue(jobs[1]['source_warnings'])
        self.assertEqual(self.qr.call_count + self.pay.call_count, 1)

    def test_old_unconfirmed_bad_order_cannot_create_payment_or_change_state(self):
        from app.models import Job, JobStatus, OutputMode
        source = self.root / 'old.epub'; source.write_bytes(fixture(missing=('OEBPS/body.xhtml',)))
        job = Job(id=uuid.uuid4().hex[:12], trace_id='offline', source_filename='old.epub',
                  input_path=str(source), access_token='offline-token', output_mode=OutputMode.simplified,
                  enable_translation=True, status=JobStatus.awaiting_confirmation)
        self.main.job_store.add(job); self.add.reset_mock()
        response = self.client.post(f'/api/v2/jobs/{job.id}/confirm-profile', json={}, headers={'X-Job-Token': 'offline-token'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.main.job_store.get(job.id).status, JobStatus.awaiting_confirmation)
        self.assert_no_effects()


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    unittest.main()
