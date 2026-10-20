import importlib.util,json,unittest
from pathlib import Path
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('quality',root/'scripts/evaluate-language-quality.py');q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)
class QualityTests(unittest.TestCase):
 def setUp(self):self.c=json.loads((root/'backend/data/quality/five-language-v1.json').read_text());self.outputs={x['id']:x['reference'] for x in self.c['cases']}
 def test_all_directions_and_five_polish_languages(self):
  pairs={(x['source_language'],x['target_language']) for x in self.c['cases'] if x['task']=='translation'};self.assertEqual(len(pairs),20);self.assertEqual(sum(x['task']=='proofreading' for x in self.c['cases']),5)
 def test_reference_alone_is_not_quality_acceptance(self):
  r=q.evaluate(self.c,self.outputs);self.assertFalse(r['passed']);self.assertTrue(all(x['status']=='human_review_pending' for x in r['cases']))
 def test_corrupt_number_missing_output_and_echo_fail(self):
  cases=self.c['cases'];self.outputs[cases[0]['id']]=cases[0]['input'];self.outputs[cases[1]['id']]='changed 50 mg';del self.outputs[cases[2]['id']];r=q.evaluate(self.c,self.outputs);self.assertTrue(all(x['status']=='failed' for x in r['cases'][:3]))
 def test_review_requires_named_reviewer_notes_and_valid_scores(self):
  reviews={x['id']:{'reviewer':'fixture reviewer','notes':'synthetic test only','scores':dict.fromkeys(self.c['rubric'],2)} for x in self.c['cases']};self.assertTrue(q.evaluate(self.c,self.outputs,reviews)['passed']);reviews[self.c['cases'][0]['id']]['scores']['meaning']=1;self.assertFalse(q.evaluate(self.c,self.outputs,reviews)['passed'])
 def test_live_runner_preserves_utf8_xhtml(self):
  spec=importlib.util.spec_from_file_location('live_quality',root/'scripts/run-language-quality.py');runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
  for text in ['简体中文','繁體中文','日本語','한국어','English']:
   for prefix in ['', '<?xml version="1.0" encoding="utf-8"?>']:
    self.assertEqual(runner.extract_polish_text((prefix+'<html><body><p>'+text+'</p></body></html>').encode()),text)
if __name__=='__main__':unittest.main()
