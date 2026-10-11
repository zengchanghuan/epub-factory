"""Offline link preservation, deterministic repair and final graph checks."""
import io
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import zipfile

from bs4 import BeautifulSoup
from app.domain.html_link_contract import preserves_link_contract, restore_link_attributes
from app.domain.chapter_reduce_service import apply_chunk_results
from app.domain.epub_link_audit import audit_epub_links
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_qa_service import audit_translated_epub_output, build_translation_qa_report
from app.engine.cleaners.semantics_translator import SemanticsTranslator


def archive(documents):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        for name, value in documents.items(): z.writestr(name, value)
    data.seek(0)
    return zipfile.ZipFile(data)


class LinkContractTests(unittest.TestCase):
    def test_bad_cache_is_rejected_and_model_attribute_drift_repaired_without_retry(self):
        source = '<p>Read this note <a id="r" href="notes.xhtml#n">one</a>.</p>'
        bad = '请阅读此注释<a id="changed" href="#wrong">一</a>。'
        translator = SemanticsTranslator(target_lang='zh-CN')
        translator._cache_get = lambda *args, **kwargs: bad
        writes = []
        translator._cache_set = lambda *args, **kwargs: writes.append(args)
        calls = []
        async def request(payload, **kwargs):
            calls.append(payload)
            return {0: bad}, {}
        translator._call_llm_json_batch = request
        result = asyncio.run(translator.translate_many_chunks_async([source]))[0]
        self.assertIsNone(result.error)
        self.assertFalse(result.cached)
        self.assertTrue(preserves_link_contract(source, result.translated_html))
        self.assertEqual(len(calls), 1)
        self.assertTrue(writes)

    def test_same_tag_counts_do_not_accept_changed_critical_attributes(self):
        source = '<p><a id="ref" href="notes.xhtml#n" epub:type="noteref" role="doc-noteref" class="note">Note</a></p>'
        for key, value in [('href', '#wrong'), ('id', 'wrong'), ('epub:type', 'footnote'), ('role', 'link'), ('class', 'other')]:
            node = BeautifulSoup(source, 'html.parser'); node.a[key] = value
            bad = str(node).replace('Note', '注释')
            self.assertFalse(preserves_link_contract(source, bad))
            translator = object.__new__(SemanticsTranslator)
            self.assertFalse(translator._preserves_inline_tags(source, bad))
            self.assertIn('html_link_attribute_mismatch', audit_translation_chunk(original_html=source, translated_html=bad).flags)
            fixed, changed = restore_link_attributes(source, bad)
            self.assertTrue(changed)
            self.assertTrue(preserves_link_contract(source, fixed))
            self.assertIn('注释', fixed)

    def test_order_and_parent_relationships_not_only_attribute_multisets(self):
        source = '<sup><a href="#one">1</a></sup><a href="#two">2</a>'
        bad = '<sup><a href="#two">1</a></sup><a href="#one">2</a>'
        self.assertFalse(preserves_link_contract(source, bad))
        moved = '<a href="#one">1</a><sup><a href="#two">2</a></sup>'
        self.assertEqual(restore_link_attributes(source, moved), (moved, False))

    def test_entities_attribute_order_and_legacy_outer_block_are_equivalent(self):
        source = '<p id="owned-by-reducer"><a href="n.xhtml?a=1&amp;b=2#x" id="r">Source</a></p>'
        target = "<a id='r' href='n.xhtml?a=1&#38;b=2#x'>译文</a>"
        self.assertTrue(preserves_link_contract(source, target))

    def test_new_link_and_missing_anchor_cannot_be_invented_or_dropped(self):
        for a, b in [('正文', '<a href="#invented">正文</a>'),
                     ('<a id="empty"></a>正文', '正文'),
                     ('<a name="legacy"></a>正文', '<a></a>正文')]:
            self.assertFalse(preserves_link_contract(a, b))

    def test_reducer_restores_source_targets_and_deduplicates_bilingual_anchors(self):
        source = b'<html><body><p id="block"><a id="r" name="old" href="#n">Source</a></p><p id="n">Note</p></body></html>'
        chunk = SimpleNamespace(locator='/html/body/p[1]', translated_html='<a id="bad" name="bad" href="#bad">译文</a>', sequence=1, chunk_id='c')
        for bilingual in (False, True):
            out = apply_chunk_results(source, [chunk], bilingual)
            soup = BeautifulSoup(out, 'html.parser')
            self.assertEqual(len(soup.find_all(id='r')), 1)
            self.assertEqual(len(soup.find_all('a', attrs={'name': 'old'})), 1)
            self.assertTrue(all(a['href'] == '#n' for a in soup.find_all('a')))
            self.assertIn('译文', soup.get_text())
            with archive({'ch.xhtml': out}) as z:
                self.assertEqual(audit_epub_links(z)['links_broken'], 0)

    def test_reducer_refuses_ambiguous_topology_instead_of_publishing_guessed_link(self):
        source = b'<html><body><p><a href="#n">Source</a></p></body></html>'
        chunk = SimpleNamespace(locator='/html/body/p[1]', translated_html='译文', sequence=1, chunk_id='c')
        with self.assertRaisesRegex(ValueError, 'link/anchor'):
            apply_chunk_results(source, [chunk], False)

    def test_cross_chapter_percent_encoded_legacy_anchors_and_external_urls(self):
        with archive({'text/a.xhtml': '<p id="back"><a href="../notes%20one.xhtml#%E6%B3%A8">注</a><a href="https://example.invalid">外链</a></p>',
                      'notes one.xhtml': '<a id="注" name="注"></a><a href="text/a.xhtml#back">返回</a>'}) as z:
            r = audit_epub_links(z)
        self.assertEqual(r['links_checked'], 3)
        self.assertEqual(r['links_broken'] + r['duplicate_anchors'] + r['links_unsafe'], 0)

    def test_media_fragment_is_not_mistaken_for_an_html_anchor(self):
        with archive({'ch.xhtml': '<a href="picture.png#xywh=0,0,20,20">图</a>', 'picture.png': b'opaque'}) as z:
            self.assertEqual(audit_epub_links(z)['links_broken'], 0)

    def test_missing_file_missing_duplicate_anchor_nested_and_unsafe_links(self):
        with archive({'ch.xhtml': '<a href="missing.xhtml">一</a><a href="#missing">二</a><a href="#x">三</a>'
                      '<i id="x"></i><b id="x"></b><a href="#x"><a href="#x">嵌套</a></a>'
                      '<a href="javascript:alert(1)">危险</a><a href="../escape.xhtml">越界</a>'}) as z:
            r = audit_epub_links(z, sample_limit=2)
        self.assertGreaterEqual(r['links_broken'], 3)
        self.assertEqual(r['duplicate_anchors'], 1)
        self.assertEqual(r['nested_links'], 1)
        self.assertEqual(r['links_unsafe'], 2)
        self.assertEqual(len(r['link_samples']), 2)

    def test_final_qa_blocks_broken_body_links_even_with_zero_failed_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'book.epub'
            with zipfile.ZipFile(p, 'w') as z: z.writestr('ch.xhtml', '<p>完整译文<a href="#lost">注</a></p>')
            for lang in ('zh-CN', 'en'):
                audit = audit_translated_epub_output(p, target_lang=lang)
                self.assertEqual(audit['status'], 'failed')
                qa = build_translation_qa_report(translation_stats={'failed_chunks': 0, 'artifact_audit': audit}, output_path=p)
                self.assertFalse(qa['can_deliver'])
                self.assertIn('artifact_links_invalid', qa['flags'])


if __name__ == '__main__': unittest.main()
