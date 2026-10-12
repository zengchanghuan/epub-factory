"""Offline acceptance-contract regressions; no book content or external I/O."""
import os, tempfile, unittest, zipfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject, ArrayObject, DecodedStreamObject
from app.domain.pdf import config, content, local, pipeline, providers, review, state
from app.infra.llm_errors import ProviderAccountUnavailable
from app.models import ErrorCode
from test_d37_pdf_pipeline import fixture, response

class AcceptanceFixTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ, {'PDF_OCR_CONCURRENCY':'1','PDF_WORK_DIR':str(self.root/'work'),'MINERU_API_KEY':'','PDF_TRANSLATION_ENABLED':'1','PDF_TRANSLATION_PRICE_PER_PAGE_CNY':'.20','PDF_DEEPSEEK_API_KEY':'fake-not-sent','PDF_OCR_MAX_REQUESTS_PER_JOB':'50'})
        self.env.start();self.source=fixture(self.root/'input.pdf',('scan','text','scan'))
    def tearDown(self):self.env.stop();self.temp.cleanup()

    def test_link_annotation_without_appearance_uses_native(self):
        fixture(self.source,('text',));writer=PdfWriter(clone_from=self.source)
        link=DictionaryObject({NameObject('/Type'):NameObject('/Annot'),NameObject('/Subtype'):NameObject('/Link'),NameObject('/Rect'):ArrayObject([NumberObject(n) for n in (20,20,100,40)])})
        writer.pages[0][NameObject('/Annots')]=ArrayObject([writer._add_object(link)])
        writer.write(self.source)
        self.assertEqual(local.inspect_pdf(self.source)['pages'][0]['route'],'local')
        link[NameObject('/AP')]=DictionaryObject();writer.write(self.source)
        self.assertEqual(local.inspect_pdf(self.source)['pages'][0]['route'],'ocr')

    def test_math_font_with_readable_letters_still_uses_ocr(self):
        w=PdfWriter();p=w.add_blank_page(400,600)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Symbol')})
        p[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):w._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(b'BT /F1 18 Tf 30 500 Td (p = x + y) Tj ET')
        p[NameObject('/Contents')]=w._add_object(stream);w.write(self.source)
        self.assertIn('math_layout_requires_ocr',local.inspect_pdf(self.source)['pages'][0]['assessment']['reasons'])

    def test_mineru_preserves_structure_without_header_fallback(self):
        raw=[{'page_idx':0,'type':k,**v} for k,v in [('header',{'text':'Heading'}),('page_number',{'text':'1'}),('text',{'text':'Paragraph'}),('code',{'code_body':'```python\nx = 2\n  y = 3\n```'}),('equation',{'text':r'$$\sqrt{x^2+y^2}$$'}),('page_footnote',{'text':'Footnote explanation'}),('chart',{'chart_caption':['Chart title']}),('table',{'table_body':'<table><tr><td>A</td><td>B</td></tr></table>'})]]
        page=content.mineru_pages(raw,1)['1']
        self.assertFalse(page.get('needs_flash'));self.assertEqual(page['blocks'][1],{'type':'code','text':'x = 2\n  y = 3'})
        self.assertIn('equation',[b['type'] for b in page['blocks']]);self.assertEqual(len(page['marginalia']),2)
        self.assertEqual(page['blocks'][-1]['rows'],[['A','B']])

    def test_cross_page_empty_block_marks_both_pages(self):
        raw=[{'page_idx':i,'type':'text','text':'Paragraph'} for i in range(3)]
        raw.append({'page_idx':1,'type':'text','text':'','bbox':[20,20,800,50]})
        pages=content.mineru_pages(raw,3)
        for p in ('1','2'):self.assertIn('cross_page_continuation',pages[p]['review_reasons'])
        self.assertFalse(pages['3'].get('needs_flash'))

    def test_invalid_mineru_schema_requires_fallback(self):
        for b in [{'type':'code','code_body':None},{'type':'chart','chart_caption':'bad'},{'type':'new-schema'}]:
            self.assertTrue(content.mineru_pages([{'page_idx':0,**b}],1)['1']['needs_flash'])
        with self.assertRaises(config.PdfError):content.mineru_pages([None],1)

    def test_missing_flag_distinct_and_never_retried(self):
        d=state.job_dir('flags');img=local.render_page(self.source,1,d/'i.jpg');f=providers.Flash(d)
        with patch.object(f,'call',return_value=response({'blocks':[{'text':'Source text'}]})) as call:
            for _ in range(2):
                with self.assertRaises(config.PageReviewRequired) as c:f.extract(img,1)
                self.assertEqual(c.exception.reason,'missing_confidence_field')
        self.assertEqual(call.call_count,1);self.assertIn('response',state.read(d/'flash-1-full.json'))
        with self.assertRaises(config.PageReviewRequired) as c:content.flash_payload('{"uncertain":true,"blocks":[]}',1)
        self.assertEqual(c.exception.reason,'uncertain_content')

    def test_invalid_content_is_not_silently_blank(self):
        for raw in ['broken','{"uncertain":false,"blocks":[{"text":" "}]}']:
            with self.assertRaises(config.PageReviewRequired):content.flash_payload(raw,1)
        self.assertEqual(content.flash_payload('{"uncertain":false,"blank":true,"blocks":[]}',1)['blocks'],[])

    def partial(self):
        pf=pipeline.preflight(self.source,'partial')
        with patch.object(providers.Flash,'call',side_effect=[response({'blocks':[{'text':'Missing flag'}]}),response()]) as call:
            artifact,summary=pipeline.prepare(self.source,'partial',pf['pdf'])
        self.assertEqual(call.call_count,2)
        return pf,artifact,summary

    def test_review_continues_later_pages_without_claiming_success(self):
        pf,out,summary=self.partial();d=state.job_dir('partial')
        self.assertEqual(summary['pages_extracted'],2);self.assertEqual(summary['pending_pages'],[{'page':1,'reason':'missing_confidence_field'}])
        self.assertTrue((d/'page-3.json').exists());self.assertFalse((d/'page-1.json').exists())
        with zipfile.ZipFile(out) as z:
            self.assertIn('OCR 内容待核验'.encode(),z.read('OEBPS/page-0001.xhtml'))
            self.assertFalse(any(name.endswith('.jpg') for name in z.namelist()))
        result=NS(validation_passed=True,translation_stats={},error_code=None,message='OK')
        pipeline.apply_review_gate(result,summary)
        self.assertFalse(result.validation_passed);self.assertFalse(result.translation_stats['deliverable']);self.assertEqual(result.error_code,ErrorCode.PARTIAL_TRANSLATION.value)
        with patch.object(providers.Flash,'call',side_effect=AssertionError('repeat charge')):pipeline.prepare(self.source,'partial',pf['pdf'])

    def test_approval_digest_and_zero_charge_resume(self):
        pf,_,_=self.partial();meta=review.inspect_response('partial',1)
        with self.assertRaises(config.PdfError):review.approve_missing_flag('partial',1,'wrong','operator','Checked')
        self.assertTrue((state.job_dir('partial')/'artifact.json').exists())
        approved=review.approve_missing_flag('partial',1,meta['response_sha256'],'operator','Checked source image and response')
        self.assertEqual(approved['source_sha256'],pf['pdf']['source_sha256'])
        with patch.object(providers.Flash,'call',side_effect=AssertionError('repeat charge')):_,s=pipeline.prepare(self.source,'partial',pf['pdf'])
        self.assertFalse(s['review_required']);self.assertEqual(s['pages_extracted'],3)
        saved=state.read(state.job_dir('partial')/'flash-1-full.json');self.assertIn('response',saved);self.assertIn('review',saved)

    def test_uncertain_content_cannot_be_approved_as_missing_field(self):
        pipeline.preflight(self.source,'uncertain');state.save(state.job_dir('uncertain')/'flash-1-full.json',{'state':'invalid','response':'{"uncertain":true,"blocks":[{"text":"Maybe"}]}'})
        m=review.inspect_response('uncertain',1)
        with self.assertRaises(config.PdfError):review.approve_missing_flag('uncertain',1,m['response_sha256'],'operator','Checked')

    def test_balance_and_unknown_requests_stop_further_calls(self):
        for name,error in [('balance',ProviderAccountUnavailable('deepseek','insufficient_balance')),('unknown',config.PdfError('unresolved request'))]:
            pf=pipeline.preflight(self.source,name)
            with patch.object(providers.Flash,'extract',side_effect=error) as call:
                with self.assertRaises(type(error)):pipeline.prepare(self.source,name,pf['pdf'])
            self.assertEqual(call.call_count,1);self.assertFalse((state.job_dir(name)/'artifact.json').exists())

    def test_review_gate_preserves_existing_failure(self):
        result=NS(validation_passed=False,translation_stats={},error_code='OTHER',message='Existing failure')
        pipeline.apply_review_gate(result,{'review_required':True,'pending_pages':[{'page':1}]})
        self.assertEqual(result.error_code,'OTHER');self.assertEqual(result.message,'Existing failure')

    def test_epub_code_formula_and_table_survive(self):
        from app.engine.chunk_extractor import extract_chunks
        from xml.etree import ElementTree as ET
        d=state.job_dir('structure');local.render_page(self.source,1,d/'page-1.jpg')
        blocks=[{'type':'code','text':'if x < 2:\n    y = 3'},{'type':'equation','text':r'\frac{1}{60}'},{'type':'table','text':'A\tB','rows':[['First cell','Second cell']]},{'type':'text','text':r'The ratio is $\frac{dy}{dx}$.'}]
        out=d/'s.epub';pipeline.build_epub([{'page':1,'label':'1','blocks':blocks}],d,out,'Offline')
        with zipfile.ZipFile(out) as z:raw=z.read('OEBPS/page-0001.xhtml');ET.fromstring(raw)
        self.assertIn(b'<pre class="pdf-code"><code>if x &lt; 2:',raw)
        chunks=extract_chunks(raw,'chapter');texts='\n'.join(c.text for c in chunks)
        self.assertIn('First cell',texts);self.assertNotIn('y = 3',texts);self.assertNotIn('frac',texts)

    def test_pdf_resume_key_keeps_healthy_chapters_but_checks_source_and_settings(self):
        import copy
        from app.domain.translation_checkpoints import book_resume_key
        pf=pipeline.preflight(self.source,'resume')
        job=NS(input_path=str(self.source),translation_stats={'translation_preflight':pf},cache_policy='reuse')
        manifest={'chapters':[{'chapter_id':'page1','file_path':'page1.xhtml','chunks':[{'html':'Pending'}]}]}
        first=book_resume_key(job,manifest)
        changed=copy.deepcopy(manifest);changed['chapters'][0]['chunks'][0]['html']='Reviewed text'
        self.assertEqual(first,book_resume_key(job,changed))
        job.target_lang='ja';self.assertNotEqual(first,book_resume_key(job,changed))
        del job.target_lang
        self.source.write_bytes(self.source.read_bytes()+b'changed')
        self.assertNotEqual(first,book_resume_key(job,changed))

    def test_qa_report_rejects_unresolved_pdf_even_without_error_code(self):
        from app.domain.translation_qa_service import build_translation_qa_report
        report=build_translation_qa_report(translation_stats={'pdf_extraction':{'pending_pages':[{'page':4}]}})
        self.assertFalse(report['can_deliver']);self.assertTrue(report['requires_manual_review'])
        self.assertIn('pdf_ocr_review_required',report['flags'])

    def test_inline_math_is_immutable_in_markers_and_text_rescue(self):
        from app.engine.cleaners.semantics_translator import SemanticsTranslator
        from app.engine.chunk_extractor import visible_text_outside_media
        from bs4 import BeautifulSoup
        tr=SemanticsTranslator.__new__(SemanticsTranslator)
        raw='<p>The ratio <code class="pdf-math">\\frac{dy}{dx}</code> increases.</p>'
        protected,mapping=tr._protect_inline_tags(raw)
        self.assertNotIn('frac',protected);self.assertEqual(tr._restore_inline_tag_markers(protected,mapping),raw)
        self.assertNotIn('frac',visible_text_outside_media(BeautifulSoup(raw,'html.parser')))

if __name__=='__main__':unittest.main()
