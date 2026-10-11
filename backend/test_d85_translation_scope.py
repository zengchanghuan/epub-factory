"""Auxiliary prose must use the same preflight, translation and reduce scope."""
import tempfile
import unittest
import zipfile
import asyncio
from unittest.mock import patch
from pathlib import Path

from ebooklib import epub
from app.domain.manifest_service import chapter_requires_translation, build_manifest
from app.domain.translation_preflight_service import _body_texts, _chapters
from app.domain.fast_translation_runner import _extract_texts_from_manifest
from app.domain.book_reduce_service import reduce_and_package
from app.domain.book_reduce_service import _sync_book_title_metadata
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_qa_service import audit_translated_epub_output


class TranslationScopeTests(unittest.TestCase):
    def test_artifact_index_scope_keeps_body_and_link_failures_visible(self):
        english='The historical explanation contains a long discussion of political events and their consequences.'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'index-scope.epub'
            for filename,expected in [('index.html','passed'),('index_split_001.html','failed'),
                                      ('chapter.html','failed'),('footnotes.html','failed')]:
                with zipfile.ZipFile(path,'w') as archive:
                    archive.writestr(filename,'<html><body><p>'+english+'</p></body></html>')
                self.assertEqual(audit_translated_epub_output(path)['status'],expected,filename)
            with zipfile.ZipFile(path,'w') as archive:
                archive.writestr('index.html','<html><body><p>'+english+'</p><a href="missing.xhtml#one">Entry</a></body></html>')
            audit=audit_translated_epub_output(path)
            self.assertEqual(audit['status'],'failed')
            self.assertGreater(audit['links_broken'],0)

    def test_translated_metadata_title_preserves_refinement_targets(self):
        book=epub.EpubBook();dc='http://purl.org/dc/elements/1.1/'
        book.add_metadata('DC','title','Original',{'id':'t1'})
        book.add_metadata('DC','title','Subtitle',{'id':'t2'})
        book.add_metadata(None,'meta','main',{'refines':'#t1','property':'title-type'})
        _sync_book_title_metadata(book,'译后的书名')
        self.assertEqual(book.metadata[dc]['title'],[('译后的书名',{'id':'t1'}),('Subtitle',{'id':'t2'})])
        self.assertEqual(book.title,'译后的书名')
        self.assertEqual(book.get_metadata(None,'meta'),[('main',{'refines':'#t1','property':'title-type'})])

    def test_bare_domain_requires_matching_anchor_destination(self):
        from app.domain.translation_residual_policy import is_preserved_reference_node, without_reference_text_nodes
        from bs4 import BeautifulSoup
        for html, expected in [
            ('<a href="https://example.test">example.test</a>', True),
            ('<a href="https://example.test/page"><span>EXAMPLE.TEST</span></a>', True),
            ('<a href="https://other.test">example.test</a>', False),
            ('<a href="https://example.test">Read the article</a>', False),
            ('<a href="https://example.test">Read <span>example.test</span></a>', False),
            ('<p>example.test</p>', False),
        ]:
            soup=BeautifulSoup(html,'html.parser');node=soup.find_all(string=True)[-1]
            self.assertEqual(is_preserved_reference_node(node),expected)
        t=SemanticsTranslator(target_lang='zh-CN');url='https://example.test'
        source=f'Read the original analysis at <a href="{url}">example.test</a>.'
        async def request(payload,**kwargs):
            self.assertEqual(len(payload),1)
            self.assertNotIn('example.test',payload[0]['html'])
            return {0:'原始分析请参见'},{}
        with patch.object(t,'_call_llm_json_batch',request):
            out,_,_=asyncio.run(t._translate_text_segments_rescue(source,'structured note'))
        self.assertIn(f'<a href="{url}">example.test</a>',out)
        self.assertFalse(t._looks_untranslated(source,out))
        self.assertTrue(t._looks_untranslated(source,source))

    def test_long_reference_does_not_hide_prose_or_create_false_english_residual(self):
        url = 'https://example.org/' + '/'.join(['political-change-and-historical-background']*4)
        link = f'<a href="{url}">{url}</a>'
        source = f'<p>Please read the complete analysis of these historical political events at {link}.</p>'
        target = f'<p>关于这些历史政治事件的完整分析请参见{link}。</p>'
        t = SemanticsTranslator(target_lang='zh-CN')
        self.assertFalse(t._looks_untranslated(source,target))
        self.assertFalse(audit_translation_chunk(original_html=source,translated_html=target).likely_untranslated)
        for wrong in [source, f'<p>{link}.</p>']:
            self.assertTrue(t._looks_untranslated(source,wrong))
            self.assertTrue(audit_translation_chunk(original_html=source,translated_html=wrong).likely_untranslated)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audit.epub'
            for html, expected in [(target,'passed'),(source,'failed')]:
                with zipfile.ZipFile(path,'w') as archive:
                    archive.writestr('chapter.xhtml','<html><body>'+html+'</body></html>')
                self.assertEqual(audit_translated_epub_output(path)['status'],expected)

    def test_structured_note_reference_is_preserved_but_prose_is_translated(self):
        with tempfile.TemporaryDirectory() as directory:
            t = SemanticsTranslator(target_lang='zh-CN', glossary={'Jane Smith': 'Jane Smith'})
            url = 'https://example.org/articles/change-of-power'
            original = f'Read the complete analysis at <a href="{url}">{url}</a>.'
            calls = []
            async def model(payload, **kwargs):
                calls.append(payload)
                self.assertTrue(all(url not in item['html'] for item in payload))
                return {item['id']: '完整分析请参见' for item in payload}, {}
            with patch.object(t, '_call_llm_json_batch', model):
                out, _, _ = asyncio.run(t._translate_text_segments_rescue(original, 'structured note'))
            self.assertEqual(len(calls), 1)
            self.assertIn(f'href="{url}">{url}</a>', out)
            self.assertIn('完整分析请参见', out)
            for value in [url, 'doi:10.1234/meaning-of-life', 'Jane Smith']:
                self.assertFalse(t._should_translate_text_node(value))
                self.assertFalse(t._text_segment_still_untranslated(value, value))
            for value in ['Read the complete analysis.', 'Jane Smith explains the theory.', f'Read {url} for details.']:
                self.assertTrue(t._should_translate_text_node(value))
                self.assertTrue(t._text_segment_still_untranslated(value, value))

    def test_scope_is_separate_from_category(self):
        for kind, filename, expected in [
            ('body', 'chapter.xhtml', True), ('appendix', 'appendix_a.xhtml', True),
            ('other', 'acknowledgments.xhtml', True), ('other', 'about_the_author.xhtml', True),
            ('other', 'footnotes.xhtml', True), ('other', 'endnotes.xhtml', True),
            ('other', 'cover.xhtml', False), ('other', 'text/COVER_front.xhtml', False),
            ('body', 'discovery.xhtml', True), ('nav', 'nav.xhtml', False),
            ('copyright', 'copyright.xhtml', False), ('index', 'index.xhtml', False),
            ('unknown', 'unknown.xhtml', False),
        ]:
            with self.subTest(kind=kind, filename=filename):
                chapter = {'chapter_kind': kind, 'file_path': filename}
                self.assertEqual(chapter_requires_translation(chapter), expected)
                self.assertEqual(chapter['chapter_kind'], kind)

    def test_preflight_and_translator_agree(self):
        chapters = [dict(chapter_kind=kind, chapter_id=name, file_path=name+'.xhtml',
                         chunks=[dict(text=name, html='<p>'+name+'</p>')])
                    for kind, name in [('body', 'body'), ('other', 'acknowledgments'),
                                       ('appendix', 'appendix_a'), ('other', 'cover'), ('index', 'index')]]
        manifest = {'chapters': chapters}
        expected = ['body', 'acknowledgments', 'appendix_a']
        self.assertEqual(_body_texts(manifest), expected)
        self.assertEqual(_extract_texts_from_manifest(manifest), expected)
        self.assertEqual([c['chapter_id'] for c in _chapters(manifest, 'faithful')], expected)

    def test_reduce_writes_auxiliary_prose_and_retains_excluded_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book = epub.EpubBook()
            book.set_identifier('auxiliary-prose')
            book.set_title('Scope regression')
            book.set_language('en')
            names = ['chapter', 'acknowledgments', 'about_the_author', 'appendix_a', 'footnotes', 'cover', 'copyright', 'index']
            for name in names:
                item = epub.EpubHtml(title=name, file_name=name+'.xhtml')
                item.content = '<html><body><p id="p1">Original prose for this document.</p></body></html>'
                book.add_item(item)
                book.spine.append(item)
            book.toc = (epub.Link('chapter.xhtml', 'Chapter', 'chapter'),)
            book.add_item(epub.EpubNav())
            book.add_item(epub.EpubNcx())
            source, target = root/'source.epub', root/'target.epub'
            epub.write_epub(str(source), book)
            manifest = build_manifest(str(source), 'scope')
            kinds = {c['file_path']: c['chapter_kind'] for c in manifest['chapters']}
            self.assertEqual(kinds['acknowledgments.xhtml'], 'other')
            self.assertEqual(kinds['appendix_a.xhtml'], 'appendix')
            requested = []
            def content(path):
                requested.append(path)
                return '<html><body><p id="p1">这是译后的完整文字。</p></body></html>'.encode()
            self.assertTrue(reduce_and_package(str(source), str(target), content))
            self.assertEqual(set(requested), {n+'.xhtml' for n in names[:5]})
            with zipfile.ZipFile(target) as archive:
                for name in names:
                    path = next(p for p in archive.namelist() if p.endswith('/'+name+'.xhtml'))
                    raw = archive.read(path).decode()
                    self.assertIn('这是译后的完整文字。' if name in names[:5] else 'Original prose', raw)


if __name__ == '__main__':
    unittest.main()
