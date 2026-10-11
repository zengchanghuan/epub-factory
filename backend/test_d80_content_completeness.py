"""No model/network: source-to-artifact completeness and delivery contracts."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from app.domain.content_completeness import capture_content_baseline, audit_content_completeness
from app.domain.translation_qa_service import build_translation_qa_report


def book(path, documents, *, folder=""):
    prefix = folder + "/" if folder else ""
    manifest = ''.join(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>'
                       for i, name in enumerate(documents))
    spine = ''.join(f'<itemref idref="c{i}"/>' for i in range(len(documents)))
    package = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">test</dc:identifier>'
               '<dc:title>测试</dc:title><dc:language>zh</dc:language></metadata>'
               f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   f'<rootfiles><rootfile full-path="{prefix}content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr(prefix + 'content.opf', package)
        for name, body in documents.items():
            z.writestr(prefix + name, '<html xmlns="http://www.w3.org/1999/xhtml" '
                       'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>测试</title></head><body>'
                       + body + '</body></html>')


class ContentCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='content-coverage-test-')
        self.addCleanup(self.tmp.cleanup)
        self.source, self.output = (Path(self.tmp.name) / n for n in ('source.epub', 'output.epub'))

    def audit(self, source, output, **kwargs):
        book(self.source, source)
        book(self.output, output, folder='EPUB')
        baseline = capture_content_baseline(self.source)
        return audit_content_completeness(baseline, self.output, **kwargs)

    def test_translation_may_change_language_length_and_inline_spans(self):
        result = self.audit({'ch.xhtml': '<h1>Title</h1><p id="x">Long English text.</p>'},
                            {'ch.xhtml': '<h1>标题</h1><p id="x"><span>中文。</span></p>'})
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['expected_units'], 2)

    def test_deleted_or_empty_first_middle_last_paragraph_blocks_delivery(self):
        for index in range(3):
            for change in ('delete', 'empty'):
                with self.subTest(index=index, change=change):
                    original = ['<p>甲</p>', '<p>乙</p>', '<p>丙</p>']
                    changed = list(original)
                    changed[index] = '' if change == 'delete' else '<p> </p>'
                    report = self.audit({'ch.xhtml': ''.join(original)}, {'ch.xhtml': ''.join(changed)})
                    self.assertEqual(report['status'], 'failed')

    def test_missing_page_cannot_hide_behind_other_page_or_new_block(self):
        report = self.audit({'ch.xhtml': '<p>甲</p>', 'second.xhtml': '<h1>乙</h1>'},
                            {'ch.xhtml': '<p>甲</p><h1>乙</h1>'})
        self.assertEqual(report['missing_documents'], 1)
        self.assertEqual(report['missing_units'], 1)

    def test_missing_identified_block_cannot_be_replaced_by_equal_count_other_block(self):
        report = self.audit({'ch.xhtml': '<p id="a">甲</p><p id="b">乙</p>'},
                            {'ch.xhtml': '<p id="a">甲</p><p id="c">丙</p>'})
        self.assertEqual(report['missing_units'], 1)

    def test_title_caption_table_footnote_pre_and_mixed_parent_prose_are_covered(self):
        for tag in ('h1', 'h6', 'figcaption', 'td', 'th', 'dd', 'dt', 'pre', 'blockquote'):
            with self.subTest(tag=tag):
                self.assertEqual(self.audit({'ch.xhtml': f'<{tag}>原文</{tag}>'},
                                            {'ch.xhtml': f'<{tag}></{tag}>'})['status'], 'failed')
        for text in ('<p class="endnote">Author (2001), doi:10.1/example</p>',
                     '<div>父级正文<p>子段落</p></div>'):
            self.assertEqual(self.audit({'ch.xhtml': text}, {'ch.xhtml': '<div><p>子段落</p></div>'})['status'], 'failed')

    def test_intentionally_retained_english_counts_as_present(self):
        original = '<p class="endnote">Author (2001), doi:10.1/example</p>'
        self.assertEqual(self.audit({'ch.xhtml': original}, {'ch.xhtml': original})['status'], 'passed')

    def test_generated_scroll_wrapper_is_transparent_but_table_text_is_required(self):
        source = '<table><tr><td><p>甲</p></td><td>乙</td></tr></table>'
        wrapped = '<div class="epub-table-wrap">' + source + '</div>'
        self.assertEqual(self.audit({'ch.xhtml': source}, {'ch.xhtml': wrapped})['status'], 'passed')
        self.assertEqual(self.audit({'ch.xhtml': source}, {'ch.xhtml': wrapped.replace('甲', '')})['status'], 'failed')
        # Arbitrary prose containers must not disappear from the inventory.
        prose = '<div class="epub-table-wrap">说明' + source + '</div>'
        self.assertEqual(self.audit({'ch.xhtml': prose}, {'ch.xhtml': wrapped})['status'], 'failed')

    def test_source_placeholder_is_counted_but_cannot_replace_real_output_prose(self):
        placeholder = '<p>原文件缺少本章节</p><p>本页仅说明原文件缺失，不代表译文；其余可用章节继续处理。</p>'
        book(self.source, {'ch.xhtml': '<p>原稿</p>', 'missing.xhtml': placeholder})
        with zipfile.ZipFile(self.source) as z:
            values = {n: z.read(n) for n in z.namelist()}
        values['missing.xhtml'] = values['missing.xhtml'].replace(b'<body>', b'<body data-epub-factory-missing-document="true">')
        with zipfile.ZipFile(self.source, 'w') as z:
            for name, raw in values.items(): z.writestr(name, raw)
        baseline = capture_content_baseline(self.source)
        self.assertEqual(baseline['source_placeholders_skipped'], 1)
        book(self.output, {'ch.xhtml': '<p>译文</p>'})
        self.assertEqual(audit_content_completeness(baseline, self.output)['status'], 'passed')
        with zipfile.ZipFile(self.output, 'w') as z:
            for name, raw in values.items():
                z.writestr(name, values['missing.xhtml'] if name == 'ch.xhtml' else raw)
        self.assertEqual(audit_content_completeness(baseline, self.output)['status'], 'failed')

    def test_empty_media_and_scripts_are_not_missing_prose(self):
        original = '<p>甲</p><p></p><div><img src="picture.png"/></div><script>code</script>'
        self.assertEqual(self.audit({'ch.xhtml': original}, {'ch.xhtml': '<p>甲</p>'})['status'], 'passed')

    def test_bilingual_original_cannot_hide_empty_translation(self):
        source = {'ch.xhtml': '<p id="x">Source text.</p>'}
        for target in ('', '<span class="epub-translated"></span>'):
            report = self.audit(source, {'ch.xhtml': '<p id="x"><span class="epub-original">Source text.</span>'
                                        + target + '</p>'}, bilingual=True)
            self.assertEqual(report['status'], 'failed')
        translated = '<p id="x"><span class="epub-original">Source text.</span><br/>' \
                     '<span class="epub-translated">译文</span></p>'
        self.assertEqual(self.audit(source, {'ch.xhtml': translated}, bilingual=True)['status'], 'passed')

    def test_navigation_exemption_is_not_an_entire_document_exemption(self):
        nav = '<nav epub:type="toc"><ol><li>目录</li></ol></nav>'
        report = self.audit({'nav.xhtml': nav + '<p>编辑说明</p>'},
                            {'nav.xhtml': '<nav epub:type="toc"></nav><p></p>'})
        self.assertEqual(report['empty_units'], 1)

    def test_same_basename_in_different_folders_is_not_a_match(self):
        report = self.audit({'A/ch.xhtml': '<p>甲</p>', 'B/ch.xhtml': '<p>乙</p>'},
                            {'A/ch.xhtml': '<p>甲</p>'})
        self.assertEqual(report['missing_documents'], 1)

    def test_baseline_and_report_do_not_contain_source_prose(self):
        secret = 'PRIVATE_MANUSCRIPT_SENTENCE'
        book(self.source, {'ch.xhtml': '<p>' + secret + '</p>'})
        baseline = capture_content_baseline(self.source)
        book(self.output, {'ch.xhtml': '<p></p>'})
        report = audit_content_completeness(baseline, self.output)
        self.assertNotIn(secret, json.dumps([baseline, report]))
        self.assertNotIn(self.tmp.name, json.dumps(report))

    def test_unreadable_source_raises_before_translation_and_missing_baseline_fails_closed(self):
        self.source.write_bytes(b'bad-epub')
        with self.assertRaises(ValueError):
            capture_content_baseline(self.source)
        self.assertEqual(audit_content_completeness(None, self.output)['status'], 'scan_error')

    def test_unreadable_artifact_fails_closed_and_samples_are_bounded(self):
        book(self.source, {'ch.xhtml': '<p>甲</p><p>乙</p>'})
        baseline = capture_content_baseline(self.source)
        self.assertEqual(audit_content_completeness(baseline, self.output)['status'], 'scan_error')
        book(self.output, {'ch.xhtml': '<p/><p/>'})
        report = audit_content_completeness(baseline, self.output, sample_limit=1)
        self.assertEqual(report['empty_units'], 2)
        self.assertEqual(len(report['samples']), 1)

    def test_qa_rejects_missing_prose_even_without_untranslated_or_failed_chunks(self):
        self.output.write_bytes(b'exists')
        for status in ('failed', 'scan_error'):
            qa = build_translation_qa_report(translation_stats={'artifact_audit': {'status': 'passed',
                'content_completeness': {'status': status, 'missing_units': 2, 'empty_units': 1}}},
                output_path=self.output)
            self.assertFalse(qa['can_deliver'])
            self.assertEqual(qa['delivery_status'], 'failed')


if __name__ == '__main__':
    unittest.main()
