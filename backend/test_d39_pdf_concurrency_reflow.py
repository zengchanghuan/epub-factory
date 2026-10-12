"""TDD contracts for bounded OCR, durable requests and conservative paragraph assembly."""
import contextvars
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from app.domain.pdf import content, state


class ConcurrencyTests(unittest.TestCase):
    def test_bounded_overlap_order_and_context(self):
        from app.domain.pdf.concurrency import ordered_pages
        scope = contextvars.ContextVar('test_scope'); scope.set('order-123')
        active = peak = 0; lock = threading.Lock(); barrier = threading.Barrier(2)
        def work(i, check):
            nonlocal active, peak
            check()
            with lock: active += 1; peak = max(peak, active)
            self.assertEqual(scope.get(), 'order-123')
            barrier.wait(timeout=3)
            time.sleep(.001 * (2-i%2))
            with lock: active -= 1
            return i
        self.assertEqual(list(ordered_pages(range(20), work, workers=2)), list(range(20)))
        self.assertEqual(peak, 2)

    def test_fatal_stops_scheduling_but_drains_inflight(self):
        from app.domain.pdf.concurrency import ordered_pages
        barrier = threading.Barrier(2); started=[]; saved=[]
        def work(i, check):
            check(); started.append(i); barrier.wait(timeout=3)
            if i == 0: raise RuntimeError('402 simulated')
            time.sleep(.03); saved.append(i); return i
        with self.assertRaisesRegex(RuntimeError, '402'):
            list(ordered_pages(range(100), work, workers=2))
        self.assertEqual(set(started), {0,1}); self.assertEqual(saved,[1])

    def test_budget_atomic_under_threads(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'budget.json'
            def reserve(_):
                try: state.reserve_request(path,7); return True
                except Exception as e:
                    from app.domain.pdf.config import PdfError
                    if isinstance(e,PdfError): return False
                    raise
            with ThreadPoolExecutor(max_workers=12) as pool:
                self.assertEqual(sum(pool.map(reserve,range(80))),7)
            self.assertEqual(state.read(path)['requests'],7)

    def test_cancel_before_work(self):
        from app.domain.pdf.concurrency import ordered_pages
        def cancelled(): raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError,'cancelled'):
            list(ordered_pages(range(3),lambda *args:self.fail('called'),workers=2,cancel=cancelled))


class PipelineParallelTests(unittest.TestCase):
    def test_fatal_peer_keeps_successful_page_checkpoint(self):
        from app.domain.pdf import pipeline, providers
        from app.infra.llm_errors import ProviderAccountUnavailable
        from test_d37_pdf_pipeline import fixture
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            'PDF_WORK_DIR':d,'MINERU_API_KEY':'','PDF_DEEPSEEK_API_KEY':'offline',
            'PDF_TRANSLATION_ENABLED':'1','PDF_TRANSLATION_PRICE_PER_PAGE_CNY':'.20',
            'PDF_OCR_CONCURRENCY':'2'}):
            source=fixture(Path(d)/'s.pdf',('scan',)*4)
            pf=pipeline.preflight(source,'drain');barrier=threading.Barrier(2);calls=[]
            def extract(image,page,check):
                calls.append(page);barrier.wait(timeout=3)
                if page==1: raise ProviderAccountUnavailable('deepseek','insufficient_balance')
                time.sleep(.02)
                return {'provider':'deepseek-flash','blocks':[{'type':'text','text':'Page two'}]}
            with patch.object(providers.Flash,'extract',side_effect=extract):
                with self.assertRaises(ProviderAccountUnavailable):pipeline.prepare(source,'drain',pf['pdf'])
            self.assertEqual(set(calls),{1,2})
            self.assertTrue((state.job_dir('drain')/'page-2.json').exists())
            self.assertFalse((state.job_dir('drain')/'artifact.json').exists())

    def test_pipeline_overlaps_ocr_preserves_order_and_checkpoints(self):
        from app.domain.pdf import pipeline, providers
        from test_d37_pdf_pipeline import fixture
        import zipfile
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            'PDF_WORK_DIR':d, 'MINERU_API_KEY':'', 'PDF_DEEPSEEK_API_KEY':'offline', 'PDF_TRANSLATION_ENABLED':'1',
            'PDF_TRANSLATION_PRICE_PER_PAGE_CNY':'.20', 'PDF_OCR_CONCURRENCY':'2'}):
            source=fixture(Path(d)/'s.pdf',('scan',)*4)
            pf=pipeline.preflight(source,'parallel')
            barrier=threading.Barrier(2)
            def extract(image, page, cancel):
                barrier.wait(timeout=3)
                return {'provider':'deepseek-flash','blocks':[{'type':'text','text':f'Page {page}'}]}
            with patch.object(providers.Flash,'extract',side_effect=extract):
                out,summary=pipeline.prepare(source,'parallel',pf['pdf'])
            self.assertEqual(summary['pages_extracted'],4)
            for n in range(1,5): self.assertTrue((state.job_dir('parallel')/f'page-{n}.json').exists())
            with zipfile.ZipFile(out) as book:
                names=[n for n in book.namelist() if n.endswith('.xhtml') and '/page-' in n]
                self.assertEqual(names,sorted(names))
            with patch.object(providers.Flash,'extract',side_effect=AssertionError('duplicate')):
                pipeline.prepare(source,'parallel',pf['pdf'])


class ReflowTests(unittest.TestCase):
    def parse(self, blocks):
        return content.flash_payload(json.dumps({'uncertain':False,'blocks':blocks}),1)['blocks']

    def test_explicit_paragraph_lines_join_and_keep_source(self):
        blocks=self.parse([{'type':'text','text':'A faithful trans-\nlation preserves words.','layout':'paragraph'}])
        self.assertEqual(blocks[0]['text'],'A faithful trans-lation preserves words.')
        self.assertEqual(blocks[0]['source_text'],'A faithful trans-\nlation preserves words.')

    def test_only_adjacent_same_paragraph_joins(self):
        blocks=self.parse([{'type':'text','text':v,'paragraph_id':pid} for v,pid in [('First line','p1'),('second line.','p1'),('Next paragraph.','p2')]])
        self.assertEqual([b['text'] for b in blocks],['First line second line.','Next paragraph.'])
        self.assertEqual(blocks[0]['source_block_indices'],[0,1])

    def test_unknown_code_math_poetry_and_lists_untouched(self):
        values=[{'type':kind,'text':text} for kind,text in [('text','Unknown\nline break'),('code','if x:\n    y=2'),('equation','x+y\n=z'),('verse','First verse\nSecond verse'),('list','1. First\n2. Second')]]
        self.assertEqual([b['text'] for b in self.parse(values)],[b['text'] for b in values])

    def test_boundary_and_bad_metadata_never_join(self):
        values=[{'type':'text','text':'First','paragraph_id':'a'}, {'type':'heading','text':'Heading'}, {'type':'text','text':'Second','paragraph_id':'a'}]
        self.assertEqual(len(self.parse(values)),3)
        from app.domain.pdf.config import PageReviewRequired
        with self.assertRaises(PageReviewRequired): self.parse([{'text':'x','paragraph_id':[]}])

if __name__=='__main__': unittest.main()
