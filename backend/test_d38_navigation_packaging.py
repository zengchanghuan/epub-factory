"""B1: serialized navigation retains non-TOC content and existing anchors."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from ebooklib import epub
from lxml import etree

from app.engine.packager import EpubPackager


class NavigationPackagingTests(unittest.TestCase):
    def test_empty_optional_navigation_is_repaired_during_real_packaging(self):
        for kind in ('landmarks', 'page-list'):
            with self.subTest(kind=kind):
                book = self.make_book()
                nav = next(item for item in book.get_items() if isinstance(item, epub.EpubNav))
                root = etree.fromstring(nav.content.encode())
                node = root.xpath(f'//*[local-name()="nav" and @epub:type="{kind}"]',
                                  namespaces={'epub': 'http://www.idpf.org/2007/ops'})[0]
                node.attrib.clear()
                node.set('{http://www.idpf.org/2007/ops}type', kind)
                node.set('hidden', 'hidden')
                for child in list(node):
                    node.remove(child)
                etree.SubElement(node, '{http://www.w3.org/1999/xhtml}ol').text = ' \n '
                nav.content = etree.tostring(root)
                source = nav.content
                output = self.package(book)
                self.assertFalse(output.xpath(f'//*[local-name()="nav" and @epub:type="{kind}"]',
                                              namespaces={'epub': 'http://www.idpf.org/2007/ops'}))
                self.assertEqual(output.xpath('string(//*[@id="entry"])'), 'Translated chapter')
                self.assertEqual(output.xpath('string(//*[@id="editor-note"])'), 'Editor note')
                self.assertEqual(nav.content, source)

    def test_empty_navigation_repair_preserves_meaningful_or_ambiguous_markup(self):
        cases = [
            '<nav epub:type="toc"><ol/></nav>',
            '<nav epub:type="landmarks toc"><ol/></nav>',
            '<nav epub:type="other"><ol/></nav>',
            '<nav epub:type="landmarks" id="referenced"><ol/></nav>',
            '<nav epub:type="landmarks"><ol id="referenced"/></nav>',
            '<nav epub:type="landmarks"><h2>Landmarks</h2><ol/></nav>',
            '<nav epub:type="landmarks">Editorial note<ol/></nav>',
            '<nav epub:type="landmarks"><ol/>Editorial note</nav>',
            '<nav epub:type="landmarks"><ol>Editorial note</ol></nav>',
            '<nav epub:type="landmarks"><ol><li><a href="chapter.xhtml">Start</a></li></ol></nav>',
            '<nav epub:type="landmarks"><ol><li/></ol></nav>',
            '<nav epub:type="landmarks"><ol><!-- preserved --></ol></nav>',
            '<nav epub:type="landmarks" aria-label="Start"><ol/></nav>',
            '<nav epub:type="landmarks"><ol/><ol/></nav>',
        ]
        for markup in cases:
            with self.subTest(markup=markup):
                root = etree.fromstring(('<body xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops">' + markup + '</body>').encode())
                before = etree.tostring(root)
                self.assertEqual(EpubPackager._remove_empty_optional_navigation(root), 0)
                self.assertEqual(etree.tostring(root), before)

    def test_empty_navigation_removal_preserves_surrounding_text_and_is_idempotent(self):
        for prefix in ('Before', '<p>Before</p>'):
            with self.subTest(prefix=prefix):
                root = etree.fromstring(('<body xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops">' + prefix +
                    '<nav epub:type="landmarks"><ol/></nav>After</body>').encode())
                self.assertEqual(EpubPackager._remove_empty_optional_navigation(root), 1)
                self.assertEqual(''.join(root.itertext()), 'BeforeAfter')
                self.assertEqual(EpubPackager._remove_empty_optional_navigation(root), 0)

    def test_translated_label_removes_only_empty_decorative_spans(self):
        book = self.make_book()
        nav = next(item for item in book.get_items() if isinstance(item, epub.EpubNav))
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(nav.content, 'xml')
        label = soup.find(id='entry')
        label.append(soup.new_tag('span', attrs={'hidden': 'hidden'}))
        label.span.string = ':'
        protected = soup.new_tag('span', attrs={'id': 'preserved-anchor'})
        label.append(protected)
        nav.content = str(soup)
        root = self.package(book)
        self.assertEqual(len(root.xpath('//*[@id="entry"]')), 1)
        self.assertEqual(len(root.xpath('//*[@id="preserved-anchor"]')), 1)
        self.assertEqual(len(root.xpath('//*[@id="entry"]//*[local-name()="span" and @hidden]')), 0)

    def make_book(self, *, toc_target='text/chapter.xhtml#start', inline_id=True):
        book = epub.EpubBook()
        book.set_identifier('offline-navigation')
        book.set_title('Navigation regression')
        book.set_language('en')
        chapter = epub.EpubHtml(uid='chapter', file_name='text/chapter.xhtml', title='Chapter')
        chapter.content = '<html><body><h1 id="start">Chapter</h1><p><a href="../nav.xhtml#entry">Back</a></p></body></html>'
        book.add_item(chapter)
        nav = epub.EpubNav(file_name='nav.xhtml')
        marker = ' id="entry"' if inline_id else ''
        nav.content = f'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
          <head><title>Navigation</title></head><body id="nav-body">
          <p id="editor-note"><a href="text/chapter.xhtml#start">Editor note</a></p>
          <nav epub:type="toc" id="original-toc"><h2>Contents</h2><ol><li>
          <a href="text/chapter.xhtml#start"><span{marker}>Original label</span></a>
          </li></ol></nav>
          <nav epub:type="page-list" id="pages"><h2>Pages</h2><ol><li><a href="text/chapter.xhtml#start">17</a></li></ol></nav>
          <nav epub:type="landmarks" id="landmarks"><h2>Landmarks</h2><ol><li><a epub:type="bodymatter" href="text/chapter.xhtml#start">Start reading</a></li></ol></nav>
          </body></html>'''
        book.add_item(nav)
        book.add_item(epub.EpubNcx())
        book.spine = ['nav', chapter]
        book.toc = [epub.Link(toc_target, 'Translated chapter', 'chapter-link')]
        return book

    def package(self, book):
        tmp = tempfile.TemporaryDirectory(prefix='epub-nav-packaging-')
        self.addCleanup(tmp.cleanup)
        output = Path(tmp.name) / 'result.epub'
        self.assertTrue(EpubPackager(book, str(output)).save())
        with zipfile.ZipFile(output) as archive:
            return etree.fromstring(archive.read('EPUB/nav.xhtml'))

    def test_non_toc_labels_body_and_original_ids_survive_real_packaging(self):
        root = self.package(self.make_book())
        ids = set(root.xpath('//@id'))
        self.assertTrue({'nav-body', 'editor-note', 'original-toc', 'entry', 'pages', 'landmarks'} <= ids)
        self.assertEqual(root.xpath('string(//*[@id="editor-note"])'), 'Editor note')
        self.assertEqual(root.xpath('string(//*[@id="pages"]//*[local-name()="a"])'), '17')
        self.assertEqual(root.xpath('string(//*[@id="landmarks"]//*[local-name()="a"])'), 'Start reading')
        self.assertEqual(root.xpath('string(//*[@id="entry"])'), 'Translated chapter')
        for anchor in root.xpath('//*[local-name()="a"]'):
            self.assertEqual(anchor.get('href'), 'text/chapter.xhtml#start')

    def test_unverifiable_toc_replacement_cannot_silently_drop_nested_ids(self):
        with tempfile.TemporaryDirectory(prefix='epub-nav-conflict-') as tmp:
            book = self.make_book(toc_target='text/chapter.xhtml')
            self.assertFalse(EpubPackager(book, str(Path(tmp) / 'result.epub')).save())

    def test_toc_without_nested_ids_can_update_while_non_toc_is_retained(self):
        root = self.package(self.make_book(toc_target='text/chapter.xhtml', inline_id=False))
        self.assertEqual(root.xpath('string(//*[@id="original-toc"]//*[local-name()="a"]/@href)'), 'text/chapter.xhtml')
        self.assertEqual(root.xpath('string(//*[@id="pages"]//*[local-name()="a"])'), '17')
        self.assertEqual(root.xpath('string(//*[@id="editor-note"])'), 'Editor note')

    def test_new_empty_nav_still_uses_the_generated_document(self):
        book = self.make_book()
        next(item for item in book.get_items() if isinstance(item, epub.EpubNav)).content = b''
        root = self.package(book)
        self.assertEqual(root.xpath('string(//*[local-name()="nav"]//*[local-name()="a"])'), 'Translated chapter')

    def test_same_destination_distinct_translated_labels_stay_distinct(self):
        book = self.make_book()
        nav = next(item for item in book.get_items() if isinstance(item, epub.EpubNav))
        nav.content = nav.content.replace('</li></ol></nav>',
            '</li><li><a href="text/chapter.xhtml#start">Second original</a></li></ol></nav>', 1)
        book.toc = [epub.Link('text/chapter.xhtml#start', '译名甲', 'first'),
                    epub.Link('text/chapter.xhtml#start', '译名乙', 'second')]
        root = self.package(book)
        self.assertEqual(root.xpath('//*[@id="original-toc"]//*[local-name()="a"]/text()')
                         + root.xpath('//*[@id="entry"]/text()'), ['译名乙', '译名甲'])

    def test_toc_depth_mismatch_with_original_ids_fails_closed(self):
        book = self.make_book()
        nav = next(item for item in book.get_items() if isinstance(item, epub.EpubNav))
        nav.content = nav.content.replace('</li></ol></nav>',
            '</li><li><a href="text/chapter.xhtml#start">Second original</a></li></ol></nav>', 1)
        book.toc = [(epub.Link('text/chapter.xhtml#start', 'Parent', 'parent'),
                     [epub.Link('text/chapter.xhtml#start', 'Child', 'child')])]
        with tempfile.TemporaryDirectory(prefix='epub-nav-depth-') as tmp:
            self.assertFalse(EpubPackager(book, str(Path(tmp) / 'result.epub')).save())

    def test_unlinked_section_label_sync_preserves_section_anchor(self):
        book = self.make_book()
        nav = next(item for item in book.get_items() if isinstance(item, epub.EpubNav))
        nav.content = nav.content.replace('<h2>Contents</h2><ol>',
            '<h2>Contents</h2><ol><li><span id="part-label">Part One</span><ol>')
        nav.content = nav.content.replace('</li></ol></nav>', '</li></ol></li></ol></nav>', 1)
        book.toc = [(epub.Section('第一部分'), [epub.Link('text/chapter.xhtml#start', '章节', 'chapter-link')])]
        root = self.package(book)
        self.assertEqual(root.xpath('string(//*[@id="part-label"])'), '第一部分')
        self.assertEqual(root.xpath('string(//*[@id="entry"])'), '章节')

    def test_generated_page_list_cannot_duplicate_an_original_id(self):
        book = self.make_book()
        nav = next(item for item in book.get_items() if isinstance(item, epub.EpubNav))
        root = etree.fromstring(nav.content.encode())
        page_list = root.xpath('//*[@id="pages"]')[0]
        page_list.getparent().remove(page_list)
        root.xpath('//*[@id="editor-note"]')[0].set('id', 'pages')
        nav.content = etree.tostring(root)
        chapter = next(item for item in book.get_items() if item.get_name() == 'text/chapter.xhtml')
        chapter.content = '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><h1 id="start">Chapter</h1><span epub:type="pagebreak" id="p17" title="17"/></body></html>'
        output = self.package(book)
        ids = output.xpath('//@id')
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(output.xpath('string(//*[@id="pages"])'), 'Editor note')
        self.assertTrue(output.xpath('//*[local-name()="nav" and @id="pages-generated"]'))


if __name__ == '__main__':
    unittest.main()
