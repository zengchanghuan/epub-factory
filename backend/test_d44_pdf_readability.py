"""Readability regressions from Chinese/math PDFs. Synthetic text; no paid calls."""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from app.domain.pdf import content, local, pipeline, providers, state, text_quality
from app.domain.pdf.math_render import MathAssets
from test_d37_pdf_pipeline import fixture, response
from test_d42_pdf_native_layout import layout_fixture


class ReadabilityTests(unittest.TestCase):
    def test_scaled_text_matrix_preserves_heading_and_wrap(self):
        with tempfile.TemporaryDirectory() as d:
            path = layout_fixture(Path(d) / 'scaled.pdf')
            writer = PdfWriter(clone_from=path)
            stream = DecodedStreamObject()
            stream.set_data(b'BT /F3 1 Tf 18 0 0 18 30 600 Tm (Values and types) Tj ET\n'
                            b'BT /F1 1 Tf 12 0 0 12 30 565 Tm (A faithful translation preserves the exact original meaning and) Tj '
                            b'0 -1.333333 Td (keeps every word in this paragraph.) Tj ET')
            writer.pages[0][NameObject('/Contents')] = writer._add_object(stream)
            writer.write(path)
            blocks = local.inspect_pdf(path)['pages'][0]['blocks']
            self.assertEqual([b['type'] for b in blocks], ['heading', 'text'])
            self.assertIn('meaning and keeps every word', blocks[1]['text'])

    def test_cjk_indent_marks_new_paragraph_even_after_full_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'cjk.pdf'
            writer = PdfWriter(); page = writer.add_blank_page(400, 600)
            from pypdf.generic import ArrayObject, TextStringObject, NumberObject
            cid = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/CIDFontType0'),
                                    NameObject('/BaseFont'): NameObject('/STSong-Light'), NameObject('/CIDSystemInfo'): DictionaryObject({
                                        NameObject('/Registry'): TextStringObject('Adobe'), NameObject('/Ordering'): TextStringObject('GB1'),
                                        NameObject('/Supplement'): NumberObject(4)})})
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type0'),
                                     NameObject('/BaseFont'): NameObject('/STSong-Light'), NameObject('/Encoding'): NameObject('/UniGB-UCS2-H'),
                                     NameObject('/DescendantFonts'): ArrayObject([writer._add_object(cid)])})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            lines = ['甲'*28, '乙'*32, '这是新的自然段。']
            commands = [f'BT /F1 10 Tf {x} {y} Td <{s.encode("utf-16be").hex()}> Tj ET' for s,x,y in zip(lines,[50,30,50],[550,530,510])]
            stream = DecodedStreamObject(); stream.set_data('\n'.join(commands).encode())
            page[NameObject('/Contents')] = writer._add_object(stream); writer.write(path)
            info = local.inspect_pdf(path)['pages'][0]
            self.assertEqual(info['route'], 'local')
            self.assertEqual([b['text'] for b in info['blocks']], [lines[0]+lines[1], lines[2]])

    def test_symbol_font_and_rare_private_glyph_are_not_trusted(self):
        for name in ['ABCDEF+KentenGeneric', 'CircleDigitalW6', 'MSAM9', 'MSBM10', 'CMMI10']:
            self.assertTrue(local.unsafe_symbol_font(name), name)
        self.assertFalse(local.unsafe_symbol_font('CMR8'))
        assessment = text_quality.assess('可靠正文'*200+'\ue000')
        self.assertEqual(assessment['route'], 'ocr')
        self.assertIn('private_use_glyph', assessment['reasons'])

    def test_reused_id_cannot_join_chinese_speakers(self):
        raw = [{'type':'text', 'layout':'paragraph', 'paragraph_id':'p1', 'text':s}
               for s in ['他走进教室。', '“现在开始。”', '她翻开书本。']]
        result = content.flash_payload(json.dumps({'uncertain':False,'blocks':raw}),1)
        self.assertEqual([b['text'] for b in result['blocks']], [b['text'] for b in raw])
        self.assertEqual(result['raw_blocks'], raw)

    def test_legacy_reflow_is_reversible_and_idempotent(self):
        old = {'provider':'deepseek-flash', 'blocks':[{'type':'text','paragraph_id':'1','text':'她坐下了。 他翻书。',
                                                    'source_text':'她坐下了。\n他翻书。'}]}
        updated = content.refresh_result(old)
        self.assertEqual(updated['blocks'][0]['text'], '她坐下了。\n\n他翻书。')
        self.assertEqual(updated['blocks'][0]['source_text'], old['blocks'][0]['source_text'])
        self.assertIs(content.refresh_result(updated), updated)
        self.assertEqual(old['blocks'][0]['text'], '她坐下了。 他翻书。')

    def test_single_character_addition_is_detected_despite_full_token_coverage(self):
        page = {'text':'下课后回到图书馆。','route':'ocr'}
        result = {'provider':'deepseek-flash','blocks':[{'text':'下课后便回到图书馆。'}]}
        diff = text_quality.reference(page, result)['comparison']
        self.assertEqual(diff['embedded_token_coverage'], 1)
        self.assertEqual(diff['status'], 'text_difference')
        self.assertEqual(diff['alignment']['inserted_chars'], 1)
        self.assertEqual(diff['alignment']['changes'][0]['extracted'], '便')
        self.assertFalse(diff['semantic_accuracy_verified'])
        self.assertEqual(result['blocks'][0]['text'], '下课后便回到图书馆。')

    def test_alignment_preserves_symbols_but_ignores_wrapping(self):
        self.assertFalse(text_quality.differences('x + y\n等于零', 'x+y 等于零')['different'])
        diff = text_quality.differences('x−y²', 'x-y2')
        self.assertTrue(diff['different']); self.assertEqual(diff['replaced_chars'], 2)
        self.assertEqual(text_quality.differences('甲'*13000, '乙')['alignment_skipped'], 'page_text_limit')

    def test_math_delimiters_and_svg_are_static_and_deduplicated(self):
        math = MathAssets()
        raw = math.inline(r'根式 $\sqrt{x}$；分式 \(\frac{1}{3}\)；平方 \[x^2\]；同式 $\sqrt{x}$。', 1)
        self.assertEqual(raw.count('<img'), 4); self.assertEqual(len(math.assets), 3)
        self.assertNotIn('<script', raw)
        for svg in math.assets.values():
            ET.fromstring(svg)
            self.assertNotIn('<text', svg); self.assertIn('<path', svg)
            self.assertNotIn('https://', svg)
        soup = BeautifulSoup(raw, 'html.parser')
        self.assertNotIn('sqrt', soup.get_text())
        self.assertIn('根式', soup.get_text())

    def test_missing_cjk_glyph_uses_mathml_without_blank_svg(self):
        math = MathAssets(); raw = math.equation(r'\sqrt{判别式}', 1)
        self.assertIn('<math ', raw); self.assertIn('<mi>判</mi>', raw)
        self.assertEqual(math.assets, {}); self.assertIn('#original-page-1', raw)

    def test_explicit_upright_math_is_not_changed_to_italic(self):
        math = MathAssets()
        math.formula('i', 1)
        math.formula(r'\mathrm{i}', 1)
        italic, upright = math.assets.values()
        self.assertNotEqual(italic, upright)

    def test_bad_math_preserves_literal_and_page_reference(self):
        math = MathAssets()
        for tex in [r'\notarealcommand{x}', '{'*40+'x'+'}'*40, 'x'*2049, 'a + b\n_______\nc + d']:
            markup = math.equation(tex, 7)
            self.assertIn('pdf-math', markup); self.assertIn('#original-page-7', markup)
            self.assertEqual(BeautifulSoup(markup,'html.parser').code.get_text(), tex)
        self.assertEqual(math.report['source_fallback'], 4)

    def test_currency_and_markup_stay_plain_text(self):
        math = MathAssets()
        raw = math.inline('Price $5 and $10; <script>alert(1)</script>', 1)
        self.assertIn('$5 and $10', raw); self.assertNotIn('<script>', raw)
        self.assertEqual(math.report['svg'], 0)

    def test_mathml_is_excluded_from_translation_and_preserved_by_markers(self):
        from app.engine.chunk_extractor import visible_text_outside_media, media_subtrees
        from app.engine.cleaners.semantics_translator import SemanticsTranslator
        math = '<math xmlns="http://www.w3.org/1998/Math/MathML"><msqrt><mi>判别式</mi></msqrt></math>'
        raw = '<p>根式为 '+math+'。</p>'
        soup = BeautifulSoup(raw,'html.parser')
        self.assertEqual(visible_text_outside_media(soup), '根式为 。')
        translator = SemanticsTranslator.__new__(SemanticsTranslator)
        protected, mapping = translator._protect_inline_tags(raw)
        self.assertNotIn('判别式', protected)
        restored = translator._restore_inline_tag_markers(protected, mapping)
        self.assertEqual(media_subtrees(restored), media_subtrees(raw))

    def test_epub_declares_mathml_language_and_preserves_original_image(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'page-1.jpg').write_bytes(b'original-fixture-image')
            blocks = [{'type':'text','text':r'中文数学示例。根式 $\sqrt{判别式}$。平方 \(x^2\)。'}]
            report = pipeline.build_epub([{'page':1,'label':'i','blocks':blocks}],root,root/'s.epub','中文',language='zh-Hans')
            self.assertEqual((report['svg'],report['mathml']), (1,1))
            with zipfile.ZipFile(root/'s.epub') as book:
                opf = book.read('OEBPS/content.opf').decode()
                self.assertIn('<dc:language>zh-Hans</dc:language>', opf)
                self.assertIn('properties="mathml"', opf)
                self.assertEqual(book.read('OEBPS/images/page-1.jpg'), b'original-fixture-image')
                for name in book.namelist():
                    if name.endswith(('.xhtml','.opf','.svg')): ET.fromstring(book.read(name))

    def test_old_artifact_and_ocr_cache_upgrade_without_model_call(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'PDF_WORK_DIR':d,'MINERU_API_KEY':'',
                    'PDF_TRANSLATION_ENABLED':'1','PDF_TRANSLATION_PRICE_PER_PAGE_CNY':'.20','PDF_DEEPSEEK_API_KEY':'offline'}):
            root = Path(d); source = fixture(root/'s.pdf',('scan',))
            pf = pipeline.preflight(source,'upgrade')
            with patch.object(providers.Flash,'call',return_value=response()) as call:
                artifact, _ = pipeline.prepare(source,'upgrade',pf['pdf'])
            self.assertEqual(call.call_count, 1)
            directory = state.job_dir('upgrade')
            meta = state.read(directory/'artifact.json'); meta.pop('version'); state.save(directory/'artifact.json',meta)
            saved = state.read(directory/'page-1.json'); saved.pop('reflow_version'); saved.pop('raw_blocks')
            saved['embedded_reference'].pop('version'); state.save(directory/'page-1.json',saved)
            with patch.object(providers.Flash,'call',side_effect=AssertionError('repeat charge')):
                pipeline.prepare(source,'upgrade',pf['pdf'])
            self.assertEqual(state.read(directory/'artifact.json')['version'], pipeline.ARTIFACT_VERSION)
            self.assertEqual(state.read(directory/'page-1.json')['reflow_version'], content.REFLOW_VERSION)
            self.assertEqual(state.read(directory/'flash-budget.json')['requests'], 1)


if __name__ == '__main__':
    unittest.main()
