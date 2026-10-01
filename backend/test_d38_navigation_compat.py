"""B1 navigation repair: offline, deterministic and intentionally fail-closed.

Real-book content/EPUBCheck gates are separately opt-in in
test_d38_navigation_history.py. These synthetic cases never call a model.
"""
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import MethodType
from unittest.mock import patch

from ebooklib import epub
from lxml import etree

from app.engine.navigation_compat import (
    ORIGINAL_HREF, WARNING_DISABLED_NAVIGATION, WARNING_REPAIRED_NAVIGATION,
    WARNING_UNRESOLVED_NAVIGATION, normalize_book_navigation,
)
from app.engine.packager import EpubPackager
from app.engine.toc_rebuilder import TocRebuilder
from app.engine.unpacker import EpubUnpacker
from app.engine.cleaners.cjk_normalizer import CjkNormalizer


def add_document(book, path, body, *, nav=False, css=None):
    item = epub.EpubNav(file_name=path) if nav else epub.EpubHtml(title=path, file_name=path)
    item.set_content(('<html xmlns="http://www.w3.org/1999/xhtml" '
                      'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Test</title>'
                      '</head><body>' + body + '</body></html>').encode())
    if css:
        item.add_link(href=css, rel='stylesheet', type='text/css')
    book.add_item(item)
    return item


def document(item):
    return etree.fromstring(item.get_content())


def hrefs(item):
    return {node.get('id'): node.get('href') for node in document(item).iter()
            if isinstance(node.tag, str) and etree.QName(node).localname == 'a'}


class NavigationCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.book = epub.EpubBook()
        self.book.set_identifier('offline-navigation-compat')
        self.book.set_title('Navigation compatibility')
        self.book.set_language('zh')

    def toc(self, body, path='text/toc.xhtml', *, nav=False):
        item = add_document(self.book, path, body, nav=nav)
        if not nav:
            self.book.guide = [{'type': 'toc', 'title': '目录', 'href': path}]
        return item

    def test_exact_unique_title_repairs_all_navigation_but_keeps_nested_valid_link(self):
        toc = self.toc('<a id="repair" href="chapter.xhtml#old">Main</a>'
                       '<a id="valid" href="chapter.xhtml#sub">Sub</a>')
        add_document(self.book, 'text/chapter.xhtml', '<h1 id="main">Main</h1><h2 id="sub">Sub</h2>')
        main, sub = epub.Link('text/chapter.xhtml#old', 'Main', 'main'), epub.Link('text/chapter.xhtml#sub', 'Sub', 'sub')
        self.book.toc = [(main, [sub])]
        self.book.guide.append({'type': 'text', 'title': 'Start', 'href': 'text/chapter.xhtml#old'})
        report = normalize_book_navigation(self.book)
        self.assertEqual(report.repaired, 3)
        self.assertEqual(report.unresolved, 0)
        self.assertEqual(hrefs(toc), {'repair': 'chapter.xhtml#main', 'valid': 'chapter.xhtml#sub'})
        self.assertEqual(main.href, 'text/chapter.xhtml#main')
        self.assertEqual(sub.href, 'text/chapter.xhtml#sub')
        self.assertIs(self.book.toc[0][1][0], sub)
        self.assertEqual(self.book.guide[-1]['href'], 'text/chapter.xhtml#main')

    def test_unmatched_title_or_duplicate_title_never_uses_lone_heading(self):
        for label, content in [('注释 2', '<h1 id="intro">Introduction</h1>'),
                               ('Conclusion', '<h2 id="sub">Introduction</h2>'),
                               ('Repeat', '<h1 id="one">Repeat</h1><h2 id="two">Repeat</h2>')]:
            with self.subTest(label=label):
                book = epub.EpubBook()
                add_document(book, 'chapter.xhtml', content)
                link = epub.Link('chapter.xhtml#absent', label, 'link')
                book.toc = [link]
                report = normalize_book_navigation(book)
                self.assertEqual(link.href, 'chapter.xhtml#absent')
                self.assertEqual(report.unresolved, 1)
                self.assertIn(WARNING_UNRESOLVED_NAVIGATION, report.warnings)

    def test_unique_matching_heading_without_id_gets_stable_anchor(self):
        chapter = add_document(self.book, 'chapter.xhtml', '<h2>Unique</h2><p>Retain all prose.</p>')
        link = epub.Link('chapter.xhtml#missing', 'Unique', 'entry')
        self.book.toc = [link]
        normalize_book_navigation(self.book)
        anchor = link.href.split('#')[1]
        self.assertTrue(anchor.startswith('epub-factory-nav-'))
        self.assertEqual(document(chapter).xpath('//*[local-name()="h2"]/@id'), [anchor])
        before = chapter.get_content()
        second = normalize_book_navigation(self.book)
        self.assertEqual((second.repaired, second.disabled, second.unresolved), (0, 0, 0))
        self.assertEqual(chapter.get_content(), before)

    def test_only_toc_scope_is_modified_in_nav_document(self):
        nav = self.toc('<nav epub:type="toc"><a id="toc" href="chapter.xhtml#old">Chapter</a>'
                       '<a id="note" epub:type="noteref" href="chapter.xhtml#old">Chapter</a></nav>'
                       '<nav epub:type="page-list"><a id="page" href="chapter.xhtml#old">Chapter</a></nav>'
                       '<nav epub:type="landmarks"><a id="landmark" href="chapter.xhtml#old">Chapter</a></nav>'
                       '<p><a id="prose" href="chapter.xhtml#old">Chapter</a></p>', path='nav.xhtml', nav=True)
        add_document(self.book, 'chapter.xhtml', '<h1 id="chapter">Chapter</h1>')
        normalize_book_navigation(self.book)
        self.assertEqual(hrefs(nav), {'toc': 'chapter.xhtml#chapter', 'note': 'chapter.xhtml#old',
                                     'page': 'chapter.xhtml#old', 'landmark': 'chapter.xhtml#old',
                                     'prose': 'chapter.xhtml#old'})

    def test_guide_html_toc_excludes_note_and_page_list_and_ordinary_prose_outside_toc(self):
        toc = self.toc('<a id="toc" href="chapter.xhtml#old">Chapter</a>'
                       '<aside epub:type="footnote"><a id="back" href="chapter.xhtml#old">Chapter</a></aside>'
                       '<nav epub:type="page-list"><a id="page" href="chapter.xhtml#old">Chapter</a></nav>')
        prose = add_document(self.book, 'text/prose.xhtml', '<p><a id="plain" href="chapter.xhtml#old">Chapter</a></p>')
        add_document(self.book, 'text/chapter.xhtml', '<h1 id="chapter">Chapter</h1>')
        normalize_book_navigation(self.book)
        self.assertEqual(hrefs(toc)['toc'], 'chapter.xhtml#chapter')
        self.assertEqual(hrefs(toc)['back'], 'chapter.xhtml#old')
        self.assertEqual(hrefs(toc)['page'], 'chapter.xhtml#old')
        self.assertEqual(hrefs(prose)['plain'], 'chapter.xhtml#old')

    def test_placeholder_disabled_without_deleting_label_structure_or_original_href(self):
        toc = self.toc('<p><a id="placeholder" class="keep" href="XXXXXXXX"><em>Keep label</em></a></p>'
                       '<a id="unknown" href="missing.xhtml">Missing chapter</a>')
        before = ''.join(document(toc).itertext())
        report = normalize_book_navigation(self.book)
        node = document(toc).xpath('//*[@id="placeholder"]')[0]
        self.assertIsNone(node.get('href'))
        self.assertEqual(node.get(ORIGINAL_HREF), 'XXXXXXXX')
        self.assertEqual(node.get('aria-disabled'), 'true')
        self.assertEqual(node.get('class'), 'keep')
        self.assertEqual(etree.QName(node[0]).localname, 'em')
        self.assertEqual(' '.join(''.join(document(toc).itertext()).split()), ' '.join(before.split()))
        self.assertEqual(hrefs(toc)['unknown'], 'missing.xhtml')
        self.assertEqual((report.disabled, report.unresolved), (1, 1))
        self.assertIn(WARNING_DISABLED_NAVIGATION, report.warnings)
        self.assertIn(WARNING_UNRESOLVED_NAVIGATION, report.warnings)

    def test_missing_regenerated_nav_target_stays_fail_closed(self):
        nav = self.toc('<nav epub:type="toc"><a id="bad" href="XXXXXXXX">Keep</a></nav>', path='nav.xhtml', nav=True)
        self.book.toc = [epub.Link('XXXXXXXX', 'Keep', 'keep')]
        report = normalize_book_navigation(self.book)
        self.assertEqual(report.disabled, 0)
        self.assertEqual(report.unresolved, 2)
        self.assertEqual(hrefs(nav)['bad'], 'XXXXXXXX')
        self.assertNotIn(WARNING_DISABLED_NAVIGATION, report.warnings)

    def test_toc_top_recovery_only_for_the_explicit_toc_guide(self):
        self.toc('<a href="chapter.xhtml">Chapter</a>')
        add_document(self.book, 'text/chapter.xhtml', '<p>Body.</p>')
        self.book.guide[0]['href'] += '#old-toc'
        self.book.toc = [epub.Link('text/toc.xhtml#missing-section', 'Imagined subsection', 'bad')]
        report = normalize_book_navigation(self.book)
        self.assertEqual(self.book.guide[0]['href'], 'text/toc.xhtml')
        self.assertEqual(self.book.toc[0].href, 'text/toc.xhtml#missing-section')
        self.assertEqual((report.repaired, report.unresolved), (1, 1))

    def test_reading_start_requires_first_visible_body_heading_and_no_other_anchor(self):
        for before, extra, expected in [('', '', '#start'), ('<p>Before heading.</p>', '', '#old'),
                                        ('', '<p id="another">Text</p>', '#old')]:
            with self.subTest(before=before, extra=extra):
                book = epub.EpubBook()
                add_document(book, 'chapter.xhtml', before + '<h2 id="start">Chapter</h2>' + extra)
                book.guide = [{'type': 'text', 'title': 'Start', 'href': 'chapter.xhtml#old'}]
                normalize_book_navigation(book)
                self.assertEqual(book.guide[0]['href'], 'chapter.xhtml' + expected)

    def test_guide_cannot_borrow_different_old_fragment_of_a_subsection(self):
        add_document(self.book, 'chapter.xhtml', '<h1 id="intro">Introduction</h1>'
                     '<p>Body</p><h2 id="five">Section Five</h2>')
        self.book.toc = [epub.Link('chapter.xhtml#section-old', 'Section Five', 'five')]
        self.book.guide = [{'type': 'text', 'title': 'Start', 'href': 'chapter.xhtml#start-old'}]
        normalize_book_navigation(self.book)
        self.assertEqual(self.book.toc[0].href, 'chapter.xhtml#five')
        self.assertEqual(self.book.guide[0]['href'], 'chapter.xhtml#start-old')

    def test_superscript_note_marker_needs_exact_tag_and_unambiguous_css_evidence(self):
        cases = [('.number {vertical-align:super}', 'span', True),
                 ('span.number {vertical-align:super}', 'span', True),
                 ('span.number {vertical-align:super}', 'em', False),
                 ('.number {font-size:small}', 'span', False),
                 ('.number {vertical-align:super} h1 > span.number {vertical-align:baseline}', 'span', False),
                 ('.number {vertical-align:super} @media all {.number {vertical-align:baseline}}', 'span', False),
                 ('.number {vertical-align:super} .number {vertical-align:baseline}', 'span', False),
                 ('@import "external.css"; .number {vertical-align:super}', 'span', False)]
        for css, tag, expected in cases:
            with self.subTest(css=css, tag=tag):
                book = epub.EpubBook()
                book.add_item(epub.EpubItem(uid='style', file_name='style.css', media_type='text/css', content=css.encode()))
                chapter = add_document(book, 'chapter.xhtml', f'<h1 id="chapter">序言<{tag} class="number">1</{tag}></h1>', css='style.css')
                link = epub.Link('chapter.xhtml#old', '序言', 'preface')
                book.toc = [link]
                before = ''.join(document(chapter).itertext())
                normalize_book_navigation(book)
                self.assertEqual(link.href, 'chapter.xhtml#chapter' if expected else 'chapter.xhtml#old')
                self.assertEqual(''.join(document(chapter).itertext()), before)

    def test_urls_valid_links_and_toc_only_fixtures_are_not_guessed(self):
        book = epub.EpubBook()
        link = epub.Link('missing.xhtml#old', 'Chapter', 'link')
        book.toc = [link]
        self.assertEqual(normalize_book_navigation(book).repaired, 0)
        self.assertEqual(link.href, 'missing.xhtml#old')
        toc = self.toc('<a id="encoded" href="../chapters/a%20b.xhtml?mode=1#old">Chapter</a>'
                       '<a id="valid" href="../chapters/a%20b.xhtml#%E7%AB%A0">Keep exact URL</a>'
                       '<a id="external" href="https://example.invalid/a#old">Chapter</a>')
        add_document(self.book, 'chapters/a b.xhtml', '<h1 id="章">Chapter</h1>')
        normalize_book_navigation(self.book)
        self.assertEqual(hrefs(toc), {'encoded': '../chapters/a%20b.xhtml?mode=1#%E7%AB%A0',
                                     'valid': '../chapters/a%20b.xhtml#%E7%AB%A0',
                                     'external': 'https://example.invalid/a#old'})

    def test_css_resets_conditional_styles_and_unknown_stylesheets_are_not_proof(self):
        cases = [('<style>.number {vertical-align:super}</style>', 'style="all:initial"'),
                 ('<style media="print">.number {vertical-align:super}</style>', ''),
                 ('<style>.number {vertical-align:super} span {all:initial}</style>', ''),
                 ('<style>.number {vertical-align:super}</style><link rel="stylesheet" href="missing.css"/>', '')]
        for styles, attributes in cases:
            with self.subTest(styles=styles, attributes=attributes):
                book = epub.EpubBook()
                item = epub.EpubHtml(title='chapter', file_name='chapter.xhtml')
                item.set_content(('<html xmlns="http://www.w3.org/1999/xhtml"><head>' + styles +
                                  '</head><body><h1 id="c">Chapter<span class="number" ' + attributes +
                                  '>1</span></h1></body></html>').encode())
                # Like normalize_book: retain actual source head/style markup,
                # which a bare ebooklib EpubHtml.get_content otherwise drops.
                item.get_content = MethodType(lambda self: self.content, item)
                book.add_item(item)
                book.toc = [epub.Link('chapter.xhtml#old', 'Chapter', 'c')]
                report = normalize_book_navigation(book)
                self.assertEqual(book.toc[0].href, 'chapter.xhtml#old')
                self.assertEqual(report.unresolved, 1)

    def test_duplicate_existing_id_is_not_a_verified_heading_anchor(self):
        add_document(self.book, 'chapter.xhtml', '<h1 id="same">Chapter</h1><p id="same">Body</p>')
        self.book.toc = [epub.Link('chapter.xhtml#old', 'Chapter', 'c')]
        self.assertEqual(normalize_book_navigation(self.book).unresolved, 1)
        self.assertEqual(self.book.toc[0].href, 'chapter.xhtml#old')

    def test_non_translation_keeps_toc_label_while_explicit_translation_syncs(self):
        add_document(self.book, 'chapter.xhtml', '<h1 id="c">Preface to an Annotated Edition</h1>')
        self.book.toc = [epub.Link('chapter.xhtml#c', 'Preface', 'c')]
        TocRebuilder().rebuild(self.book)
        self.assertEqual(self.book.toc[0].title, 'Preface')
        TocRebuilder().rebuild(self.book, target_lang='zh-CN')
        self.assertEqual(self.book.toc[0].title, '序言')
        self.book.toc[0].title = '版權頁'
        normalizer = CjkNormalizer(lexicon_domains=[], enable_proper_noun=False)
        from bs4 import BeautifulSoup
        TocRebuilder().rebuild(self.book, title_normalizer=lambda value: BeautifulSoup(
            normalizer.process(f'<span>{value}</span>'.encode(), 9), 'html.parser').get_text())
        self.assertEqual(self.book.toc[0].title, '版权页')

    def test_reduce_boundary_can_explicitly_sync_legacy_translated_titles_without_target_lang(self):
        add_document(self.book, 'chapter.xhtml', '<h1 id="c">回写后的译文标题</h1>')
        self.book.toc = [epub.Link('chapter.xhtml#c', 'Source heading', 'c')]
        TocRebuilder().rebuild(self.book)
        self.assertEqual(self.book.toc[0].title, 'Source heading')
        TocRebuilder().rebuild(self.book, synchronize_titles=True)
        self.assertEqual(self.book.toc[0].title, '回写后的译文标题')

    def test_repaired_book_packages_consistent_ncx_nav_guide_and_persistent_warnings(self):
        toc = self.toc('<a href="chapter.xhtml#old">Chapter</a><a href="XXXXXXXX"><em>Placeholder</em></a>')
        chapter = add_document(self.book, 'text/chapter.xhtml', '<h1 id="chapter">Chapter</h1><p>All content.</p>')
        self.book.toc = [epub.Link('text/chapter.xhtml#old', 'Chapter', 'entry')]
        self.book.guide.append({'type': 'text', 'title': 'Start', 'href': 'text/chapter.xhtml#old'})
        self.book.spine = [toc, chapter]
        self.book.add_item(epub.EpubNcx())
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'source.epub', Path(temp) / 'output.epub'
            epub.write_epub(str(source), self.book)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            unpacker = EpubUnpacker(str(source))
            book = unpacker.load_book()
            self.assertIsNotNone(book)
            self.assertEqual(set(unpacker.source_warnings), {WARNING_REPAIRED_NAVIGATION, WARNING_DISABLED_NAVIGATION})
            self.assertTrue(EpubPackager(book, str(output)).save())
            with zipfile.ZipFile(output) as archive:
                ncx = etree.fromstring(archive.read('EPUB/toc.ncx'))
                nav = etree.fromstring(archive.read('EPUB/nav.xhtml'))
                opf = etree.fromstring(archive.read('EPUB/content.opf'))
                self.assertEqual(ncx.xpath('//*[local-name()="content"]/@src'), ['text/chapter.xhtml#chapter'])
                self.assertIn('text/chapter.xhtml#chapter', nav.xpath('//*[local-name()="a"]/@href'))
                self.assertNotIn('#old', archive.read('EPUB/nav.xhtml').decode())
                self.assertIn('text/chapter.xhtml#chapter', opf.xpath('//*[local-name()="reference"]/@href'))
            second = EpubUnpacker(str(output))
            self.assertIsNotNone(second.load_book())
            self.assertEqual(set(second.source_warnings), set(unpacker.source_warnings))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)

    def test_legacy_translation_pipeline_explicitly_syncs_translated_heading(self):
        # A wiring test, not an EPUBCheck gate or real-model quality claim.
        from app.engine.compiler import ExtremeCompiler
        from app.engine.cleaners.semantics_translator import SemanticsTranslator
        chapter = add_document(self.book, 'chapter.xhtml', '<h1 id="c">SourceHeading</h1><p>Body.</p>')
        self.book.toc = [epub.Link('chapter.xhtml#c', 'SourceHeading', 'c')]
        self.book.spine = [chapter]
        self.book.add_item(epub.EpubNcx())
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'source.epub', Path(temp) / 'output.epub'
            epub.write_epub(str(source), self.book)
            compiler = ExtremeCompiler(str(source), str(output), enable_translation=True,
                                       target_lang='zh-CN', lexicon_domains=[], enable_proper_noun=False)
            def translate(translator, content, _item_type):
                translator.stats.total_chunks += 1
                translator.stats.translated_chunks += 1
                return content.replace(b'SourceHeading', '明确译文标题'.encode())
            with patch.object(compiler, '_build_and_inject_auto_glossary'), \
                 patch.object(compiler, '_validate_output'), \
                 patch.object(SemanticsTranslator, 'process', new=translate):
                self.assertTrue(compiler._run_full_pipeline())
            self.assertEqual(compiler.book.toc[0].title, '明确译文标题')
            self.assertEqual(compiler.get_translation_stats()['api_calls'], 0)

    def test_reduce_new_warning_reaches_caller_and_persists_in_output(self):
        from app.domain.book_reduce_service import reduce_and_package
        chapter = add_document(self.book, 'chapter.xhtml', '<h1 id="original">Chapter</h1><p>Body.</p>')
        self.book.toc = [epub.Link('chapter.xhtml#original', 'Chapter', 'c')]
        self.book.spine = [chapter]
        self.book.add_item(epub.EpubNcx())
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'source.epub', Path(temp) / 'output.epub'
            epub.write_epub(str(source), self.book)
            warnings = []
            content = chapter.get_content().replace(b'id="original"', b'id="new"')
            self.assertTrue(reduce_and_package(str(source), str(output), lambda _path: content,
                                               source_warnings=warnings))
            self.assertEqual(warnings, [WARNING_REPAIRED_NAVIGATION])
            unpacker = EpubUnpacker(str(output))
            self.assertIsNotNone(unpacker.load_book())
            self.assertEqual(unpacker.source_warnings, warnings)

    def test_compiler_single_chinese_toc_labels_are_decoded_as_utf8(self):
        # Regression for real-history labels: auto-detecting one UTF-8 Chinese
        # character as Latin-1 produced mojibake despite valid EPUB markup.
        from app.engine.compiler import ExtremeCompiler
        labels = ['一', '二', '三', '四', '五', '導言']
        expected = ['一', '二', '三', '四', '五', '导言']
        chapter = add_document(self.book, 'chapter.xhtml', ''.join(
            f'<h2 id="n{index}">{label}</h2><p>正文。</p>' for index, label in enumerate(labels)))
        self.book.toc = [epub.Link(f'chapter.xhtml#n{index}', label, f'n{index}')
                         for index, label in enumerate(labels)]
        self.book.spine = [chapter]
        self.book.add_item(epub.EpubNcx())
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'source.epub', Path(temp) / 'output.epub'
            epub.write_epub(str(source), self.book)
            compiler = ExtremeCompiler(str(source), str(output), enable_translation=False,
                                       lexicon_domains=[], enable_proper_noun=False)
            with patch.object(compiler, '_validate_output'):
                self.assertTrue(compiler._run_full_pipeline())
            self.assertEqual([entry.title for entry in compiler.book.toc], expected)
            with zipfile.ZipFile(output) as archive:
                ncx = etree.fromstring(archive.read('EPUB/toc.ncx'))
                self.assertEqual(ncx.xpath('//*[local-name()="navLabel"]/*[local-name()="text"]/text()'), expected)


if __name__ == '__main__':
    unittest.main()
