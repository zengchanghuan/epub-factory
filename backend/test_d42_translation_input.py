"""R4 real adapters, deterministic identity and local-only input contract."""
import base64
import hashlib
import os
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from lxml import etree

with patch('dotenv.load_dotenv', return_value=False):
    from app.cancellation import JobCancelled
    from app.domain import translation_input as service
    from app.domain.manifest_service import build_manifest
    from app.engine.adapters import docx_adapter, html_to_epub_builder, markdown_adapter


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')
DATA_IMAGE = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'


def make_docx(path, *, image=False, external=False, header=None, extra='', missing_image=False):
    drawing = ('<w:p><w:r><w:drawing><wp:inline><wp:extent cx="914400" cy="914400"/>'
               '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:'
               + ('link' if external else 'embed') + '="rImg"/></pic:blipFill></pic:pic>'
               '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>') if image else ''
    document = (f'<w:document xmlns:w="{W}" xmlns:r="{R}" '
                'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                '<w:body><w:p><w:r><w:t>Original English paragraph.</w:t></w:r></w:p>'
                + drawing + extra + '<w:sectPr>'
                + ('<w:headerReference w:type="default" r:id="rHeader"/>' if header is not None else '')
                + '</w:sectPr></w:body></w:document>')
    relations = ''
    if image:
        relations += (f'<Relationship Id="rImg" Type="{R}/image" Target="'
                      + ('https://example.invalid/image.png" TargetMode="External' if external else 'media/image.png') + '"/>')
    if header is not None:
        relations += f'<Relationship Id="rHeader" Type="{R}/header" Target="header1.xml"/>'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="png" ContentType="image/png"/>'
                         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                         '</Types>')
        archive.writestr('_rels/.rels', f'<Relationships xmlns="{PKG}"><Relationship Id="rDoc" Type="{R}/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', document)
        archive.writestr('word/_rels/document.xml.rels', f'<Relationships xmlns="{PKG}">{relations}</Relationships>')
        if image and not external and not missing_image:
            archive.writestr('word/media/image.png', PNG)
        if header is not None:
            archive.writestr('word/header1.xml', f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>')


def replace_docx_image(path, svg):
    """Synthetic fixture rewrite only; exercise real mammoth SVG data output."""
    with zipfile.ZipFile(path) as archive:
        entries = {item.filename: archive.read(item) for item in archive.infolist()}
    entries['[Content_Types].xml'] = entries['[Content_Types].xml'].replace(
        b'Extension="png" ContentType="image/png"', b'Extension="svg" ContentType="image/svg+xml"')
    entries['word/_rels/document.xml.rels'] = entries['word/_rels/document.xml.rels'].replace(b'image.png', b'image.svg')
    del entries['word/media/image.png']
    entries['word/media/image.svg'] = svg.encode()
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


class TranslationInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {'EPUB_FAST_TRANSLATION': '1'})
        env.start(); self.addCleanup(env.stop)
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('Network prohibited'))
        network.start(); self.addCleanup(network.stop)

    def source(self, name='upload.md', content=b'An original paragraph.'):
        path = self.root / name
        path.write_bytes(content)
        return path

    def normalize(self, path, **kwargs):
        return service.normalized_translation_input(path, **kwargs)

    def assert_reason(self, reason, path, **kwargs):
        with self.assertRaises(service.TranslationInputError) as caught:
            with self.normalize(path, **kwargs):
                self.fail('Invalid input entered executor scope')
        self.assertEqual(caught.exception.reason, reason)

    def test_disabled_gate_before_io(self):
        for value in ('0', 'false', 'NO'):
            with self.subTest(value=value), patch.dict(os.environ, {'EPUB_FAST_TRANSLATION': value}):
                self.assert_reason('service_disabled', self.root / 'missing.md')

    def test_filename_gate_is_early_and_narrow(self):
        for suffix in ('.pdf', '.mobi', '.azw3', '.txt', ''):
            self.assert_reason('unsupported_input', self.root / ('missing' + suffix))
        for suffix in ('.EPUB', '.DOCX', '.md', '.markdown'):
            service.validate_translation_filename('book' + suffix)

    def test_epub_passthrough_preserves_original_and_hash(self):
        source = self.source('source.epub', b'ORIGINAL READ ONLY')
        with self.normalize(source) as result:
            self.assertEqual(result.epub_path, source)
            self.assertEqual(result.adapter, 'epub')
            self.assertEqual(result.source_sha256, hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(source.read_bytes(), b'ORIGINAL READ ONLY')
        with self.assertRaisesRegex(RuntimeError, 'caller failure'):
            with self.normalize(source):
                raise RuntimeError('caller failure')
        self.assertTrue(source.exists())

    def test_missing_source_is_public_input_error(self):
        for suffix in ('.epub', '.md'):
            self.assert_reason('invalid_input', self.root / ('missing' + suffix))

    def test_markdown_real_adapter_stable_bytes_and_manifest(self):
        first = self.source('job-a.md')
        second = self.source('job-b.md', first.read_bytes())
        with self.normalize(first, source_name='Original Book.md') as a, self.normalize(second, source_name='Original Book.md') as b:
            self.assertNotEqual(a.epub_path, b.epub_path)
            self.assertEqual(a.epub_path.read_bytes(), b.epub_path.read_bytes())
            self.assertEqual(a.source_sha256, b.source_sha256)
            manifest_a = build_manifest(a.epub_path, 'normalization-a')
            manifest_b = build_manifest(b.epub_path, 'normalization-b')
            self.assertEqual(manifest_a['chapters'], manifest_b['chapters'])
            with zipfile.ZipFile(a.epub_path) as archive:
                opf = archive.read('OEBPS/content.opf').decode()
                self.assertIn('Original Book', opf)
                self.assertNotIn('job-a', opf)
                self.assertIn('urn:sha256:', opf)
                self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in archive.infolist()))
                self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)
            output_a, output_b = a.epub_path, b.epub_path
        self.assertFalse(output_a.parent.exists())
        self.assertFalse(output_b.parent.exists())

    def test_markdown_metadata_and_links_preserved(self):
        source = self.source(content=b'---\ntitle: Real title\nauthor: Author\nlanguage: en\nidentifier: stable-book\n---\n\n# Chapter\n\n[Reference](https://example.invalid/book) and **text**.')
        with self.normalize(source) as result, zipfile.ZipFile(result.epub_path) as archive:
            opf = archive.read('OEBPS/content.opf').decode()
            content = archive.read('OEBPS/chapter1.xhtml').decode()
            for value in ('Real title', 'Author', 'stable-book', '>en<'):
                self.assertIn(value, opf)
            self.assertIn('href="https://example.invalid/book"', content)
            self.assertIn('<strong>text</strong>', content)

    def test_different_source_changes_normalization_identity(self):
        a = self.source('a.md', b'First paragraph.')
        b = self.source('b.md', b'Second paragraph.')
        with self.normalize(a, source_name='book.md') as first, self.normalize(b, source_name='book.md') as second:
            self.assertNotEqual(first.source_sha256, second.source_sha256)
            self.assertNotEqual(first.epub_path.read_bytes(), second.epub_path.read_bytes())

    def test_cleanup_on_caller_exception_and_cancel_after_adapter(self):
        source = self.source()
        with self.assertRaisesRegex(RuntimeError, 'caller failure'):
            with self.normalize(source) as result:
                output = result.epub_path
                raise RuntimeError('caller failure')
        self.assertFalse(output.parent.exists())
        cancelled = False
        original = markdown_adapter.md_to_html
        temporary_paths = []
        def adapt(path):
            nonlocal cancelled
            temporary_paths.append(path)
            answer = original(path)
            cancelled = True
            return answer
        with patch.object(markdown_adapter, 'md_to_html', side_effect=adapt), self.assertRaises(JobCancelled):
            with self.normalize(source, cancel_check=lambda: cancelled):
                self.fail('Cancelled input entered executor')
        self.assertFalse(temporary_paths[0].parent.exists())
        self.assertTrue(source.exists())

    def test_invalid_utf8_or_no_body_rejected(self):
        self.assert_reason('invalid_input', self.source('broken.md', b'Broken \xff bytes.'))
        self.assert_reason('no_body_text', self.source('empty.md', b' \n'))

    def test_external_or_local_markdown_resources_rejected(self):
        for resource in ('image.png', 'https://example.invalid/image.png', 'file:///tmp/secret.png'):
            with self.subTest(resource=resource):
                self.assert_reason('unsupported_resource', self.source(content=f'Paragraph.\n\n![image]({resource})'.encode()))

    def test_embedded_markdown_image_is_preserved(self):
        source = self.source(content=f'Paragraph.\n\n![image]({DATA_IMAGE})'.encode())
        with self.normalize(source) as result, zipfile.ZipFile(result.epub_path) as archive:
            content = archive.read('OEBPS/chapter1.xhtml')
            self.assertIn(DATA_IMAGE.encode(), content)
            etree.fromstring(content)

    def test_css_srcset_active_and_invalid_images_rejected(self):
        for body in ('<p>Text<img srcset="one.png 1x,two.png 2x"/></p>',
                     '<p style="background:url(image.png)">Text</p>',
                     '<style>@import "style.css";</style><p>Text</p>',
                     '<p>Text<script>run()</script></p>',
                     '<p>Text<img src="data:image/png;base64,YmFk"/></p>'):
            with self.subTest(body=body), self.assertRaises(service.TranslationInputError):
                service._check_html_resources(body)

    def test_docx_real_mammoth_embedded_image_preserved(self):
        source = self.root / 'upload.docx'
        make_docx(source, image=True)
        before = source.read_bytes()
        with self.normalize(source, source_name='Original.docx') as result, zipfile.ZipFile(result.epub_path) as archive:
            self.assertEqual(result.adapter, 'docx')
            content = archive.read('OEBPS/chapter1.xhtml')
            self.assertIn(b'Original English paragraph.', content)
            self.assertIn(DATA_IMAGE.encode(), content)
            self.assertIn(b'>Original<', archive.read('OEBPS/content.opf'))
        self.assertEqual(source.read_bytes(), before)

    def test_docx_svg_external_styles_rejected_after_real_mammoth(self):
        source = self.root / 'svg.docx'
        cases = (
            '<style>@import url("https://example.invalid/remote.css");</style>',
            '<style>@import "remote.css";</style>',
            '<style>@import"https://example.invalid/remote.css";</style>',
            '<style>.p { fill: url(https://example.invalid/image.svg); }</style>',
            '<style>@font-face {font-family: x; src:url(remote.woff2);}</style>',
            '<style>.label{background-image:image-set("remote.png" 1x)}</style>',
            '<style>.label{background-image:-webkit-image-set("remote.png" 1x)}</style>',
            '<style>.label{background-image:image("remote.png")}</style>',
            '<text style="fill:url(remote.svg)">Label</text>',
            '<text fill="url(https://example.invalid/image.svg)">Label</text>',
            '<text fill="url(https://example.invalid/image.svg?x=1;y=2)">Label</text>',
            '<text style="fill:URL(//example.invalid/image.svg);">Label</text>',
            '<text style="fill:url(\\68 ttps://example.invalid/image.svg)">Label</text>',
            '<text style="fill:ur\\6c (remote.svg)">Label</text>',
            '<text style="fill:url(remote.svg">Label</text>',
        )
        for content in cases:
            with self.subTest(content=content):
                make_docx(source, image=True)
                replace_docx_image(source, '<svg xmlns="http://www.w3.org/2000/svg">' + content + '</svg>')
                before = source.read_bytes()
                self.assert_reason('unsupported_resource', source)
                self.assertEqual(source.read_bytes(), before)

    def test_docx_svg_processing_instruction_or_active_content_rejected(self):
        source = self.root / 'active.svg.docx'
        cases = [('<?xml-stylesheet href="https://example.invalid/style.css" type="text/css"?>', '', ''),
                 ('', '', '<script>run()</script>'), ('', '', '<foreignObject/>'),
                 ('', 'onload="run()"', '')]
        cases += [('', '', f'<{tag} attributeName="href" to="https://example.invalid/image.png"/>')
                  for tag in ('animate', 'animateMotion', 'animateTransform', 'set', 'discard')]
        for prefix, attrs, content in cases:
            with self.subTest(prefix=prefix, attrs=attrs, content=content):
                make_docx(source, image=True)
                replace_docx_image(source, prefix + '<svg xmlns="http://www.w3.org/2000/svg" ' + attrs + '>' + content + '</svg>')
                self.assert_reason('unsupported_resource', source)

    def test_docx_svg_internal_fragments_and_plain_styles_preserved(self):
        source = self.root / 'svg.docx'
        make_docx(source, image=True)
        svg = ('<svg xmlns="http://www.w3.org/2000/svg"><style>.label{fill:blue}</style>'
               '<defs><linearGradient id="g"/></defs><text fill="url(#g)" style="stroke: none">Label</text></svg>')
        replace_docx_image(source, svg)
        with self.normalize(source) as result, zipfile.ZipFile(result.epub_path) as archive:
            self.assertIn(base64.b64encode(svg.encode()), archive.read('OEBPS/chapter1.xhtml'))

    def test_docx_external_or_missing_image_rejected_before_adapter(self):
        source = self.root / 'source.docx'
        for options in ({'external': True}, {'missing_image': True}):
            make_docx(source, image=True, **options)
            with patch.object(docx_adapter, 'docx_to_html') as adapter:
                self.assert_reason('unsupported_resource', source)
                adapter.assert_not_called()

    def test_docx_unpreserved_images_cannot_silently_succeed(self):
        source = self.root / 'source.docx'
        make_docx(source, image=True)
        with patch.object(docx_adapter, 'docx_to_html', return_value=('<p>Text only</p>', {})):
            self.assert_reason('unsupported_resource', source)

    def test_docx_header_text_is_observably_dropped_and_rejected(self):
        source = self.root / 'source.docx'
        make_docx(source, header='Important header prose')
        body, _ = docx_adapter.docx_to_html(source)
        self.assertNotIn('Important header prose', body)
        self.assert_reason('unsupported_content', source)
        make_docx(source, header='')
        with self.normalize(source) as result:
            self.assertTrue(result.epub_path.exists())

    def test_docx_supported_textbox_is_preserved_not_blanket_rejected(self):
        source = self.root / 'textbox.docx'
        make_docx(source, extra='<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
                  '<v:textbox><w:txbxContent><w:p><w:r><w:t>Important textbox prose.</w:t></w:r></w:p>'
                  '</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>')
        with self.normalize(source) as result, zipfile.ZipFile(result.epub_path) as archive:
            self.assertIn(b'Important textbox prose.', archive.read('OEBPS/chapter1.xhtml'))

    def test_cancel_before_io_and_during_copy(self):
        with self.assertRaises(JobCancelled):
            with self.normalize(self.root / 'missing.md', cancel_check=lambda: True):
                self.fail('Cancelled input entered executor')
        source = self.source('large.md', b'A' * (2 * 1024 * 1024))
        checks = 0
        def cancel():
            nonlocal checks
            checks += 1
            return checks == 3
        original_temp = service.tempfile.TemporaryDirectory
        directories = []
        def temporary(**kwargs):
            directory = original_temp(dir=self.root, **kwargs)
            directories.append(Path(directory.name))
            return directory
        with patch.object(service.tempfile, 'TemporaryDirectory', side_effect=temporary), self.assertRaises(JobCancelled):
            with self.normalize(source, cancel_check=cancel):
                self.fail('Cancelled input entered executor')
        self.assertTrue(directories)
        self.assertTrue(all(not directory.exists() for directory in directories))
        self.assertEqual(source.stat().st_size, 2 * 1024 * 1024)

    def test_docx_duplicate_or_traversal_archive_members_rejected(self):
        source = self.root / 'unsafe.docx'
        for name in ('../outside.xml', '/absolute.xml', 'word/document.xml'):
            make_docx(source)
            with zipfile.ZipFile(source, 'a') as archive:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr(name, '<element/>')
            self.assert_reason('invalid_input', source)

    def test_docx_unknown_embedded_content_and_math_rejected(self):
        source = self.root / 'source.docx'
        for content in ('<w:altChunk r:id="rExternal"/>', '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>x</m:t></m:r></m:oMath>'):
            make_docx(source, extra=content)
            self.assert_reason('unsupported_content', source)

    def test_adapter_errors_distinguish_service_from_source(self):
        source = self.source()
        error = RuntimeError('private adapter details')
        for cause, reason in ((ValueError(), 'invalid_input'), (ModuleNotFoundError(), 'service_disabled')):
            error.__cause__ = cause
            with patch.object(markdown_adapter, 'md_to_html', side_effect=error):
                self.assert_reason(reason, source)
        with patch.object(markdown_adapter, 'md_to_html', side_effect=JobCancelled('cancelled')), self.assertRaises(JobCancelled):
            with self.normalize(source):
                self.fail('Cancelled input entered executor')

    def test_builder_default_remains_non_deterministic_opt_in(self):
        ordinary = self.root / 'ordinary.epub'
        stable = self.root / 'stable.epub'
        with patch.object(html_to_epub_builder.uuid, 'uuid4', return_value='ordinary-uuid') as make_uuid:
            html_to_epub_builder.build('<p>Text</p>', {}, ordinary)
            html_to_epub_builder.build('<p>Text</p>', {}, stable, deterministic=True)
            make_uuid.assert_called_once()
        with zipfile.ZipFile(ordinary) as archive:
            self.assertIn(b'ordinary-uuid', archive.read('OEBPS/content.opf'))
        with zipfile.ZipFile(stable) as archive:
            self.assertIn(b'urn:sha256:', archive.read('OEBPS/content.opf'))


if __name__ == '__main__':
    unittest.main()
