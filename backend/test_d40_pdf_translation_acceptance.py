"""Final EPUB and SIGKILL recovery, with actual SDK parsing but no paid HTTP calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

from pdf_acceptance_fixture import run, TEXTS, TRANSLATIONS


class FormalAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def assert_artifact(self, report):
        self.assertTrue(report['passed'], report['message'])
        self.assertEqual(report['stats']['failed_chunks'],0)
        self.assertTrue(report['stats']['qa_report']['can_deliver'])
        self.assertEqual(report['usage']['coverage'],'complete')
        self.assertEqual(report['usage']['pending_requests'],0)
        from decimal import Decimal
        calls=3+len((self.root/'requests.jsonl').read_text().splitlines())
        self.assertEqual(report['usage']['requests'],calls)
        self.assertEqual(report['usage']['priced_requests'],calls)
        self.assertEqual(report['usage']['prompt_tokens'],100*calls)
        self.assertEqual(report['usage']['completion_tokens'],30*calls)
        self.assertEqual(Decimal(report['usage']['calculated_totals']['CNY']), Decimal('0.0002832')*calls)
        with zipfile.ZipFile(self.root/'translated.epub') as book:
            content='\n'.join(book.read(n).decode() for n in book.namelist() if n.endswith('.xhtml'))
            for text in TRANSLATIONS: self.assertIn(text,content)
            for text in TEXTS: self.assertNotIn(text,content)
            self.assertIn('y = 3',content); self.assertIn(r'\frac{1}{60}',content)
            for page in range(1,4): self.assertIn(f'pdf-page-{page}',content)
            with zipfile.ZipFile(self.root/'pdf/acceptance/source.epub') as source:
                originals=sorted(source.read(n) for n in source.namelist() if n.endswith('.jpg'))
            images=sorted(book.read(n) for n in book.namelist() if n.endswith('.jpg'))
            self.assertEqual(images,originals)

    def test_final_translated_epub_cost_and_free_repeat(self):
        first=run(self.root);self.assert_artifact(first)
        calls=(self.root/'requests.jsonl').read_text()
        again=run(self.root);self.assert_artifact(again)
        self.assertEqual((self.root/'requests.jsonl').read_text(),calls)
        self.assertEqual(first['usage']['calculated_totals'],again['usage']['calculated_totals'])
        self.assertGreater(again['stats']['checkpoint_resumed_chunks'],0)

    def test_real_process_kill_then_resume_from_completed_chunk(self):
        command=[sys.executable,str(Path(__file__).with_name('pdf_acceptance_fixture.py')),str(self.root),'--pause']
        with (self.root/'child.log').open('w') as log:
            child=subprocess.Popen(command,stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+60
                while not (self.root/'paused.json').exists():
                    if child.poll() is not None: self.fail('worker exited before checkpoint: '+(self.root/'child.log').read_text()[-2500:])
                    if time.monotonic()>deadline: self.fail('checkpoint timeout')
                    time.sleep(.05)
                child.kill();child.wait(timeout=5)
            finally:
                if child.poll() is None: child.kill();child.wait(timeout=5)
        before=(self.root/'requests.jsonl').read_text()
        # Prove durable chunk checkpoints, not merely the translation cache, recover.
        (self.root/'cache.db').unlink(missing_ok=True)
        report=run(self.root);self.assert_artifact(report)
        self.assertGreater(report['stats']['checkpoint_resumed_chunks'],0)
        later=(self.root/'requests.jsonl').read_text()[len(before):]
        # Neighbor context may contain Alpha; only the actual html field is billable input.
        for row in later.splitlines():
            raw=json.loads(row)['input']
            if raw.startswith('['):
                for item in json.loads(raw): self.assertNotIn(TEXTS[0],item.get('html',''))

if __name__=='__main__':unittest.main()
