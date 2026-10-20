"""Offline scoring of supplied model outputs; never calls a provider or reads credentials."""
import argparse
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def evaluate(corpus, outputs, reviews=None):
    reviews=reviews or {};rows=[]
    for case in corpus['cases']:
        value=outputs.get(case['id']);checks=[]
        if not isinstance(value,str) or not value.strip():checks.append('missing_output')
        else:
            checks.extend('missing_literal:'+literal for literal in case['required_literals'] if literal not in value)
            if case['task']=='translation' and value.strip()==case['input'].strip():checks.append('unchanged_source')
            if '<script' in value.lower():checks.append('unexpected_script_markup')
        review=reviews.get(case['id'],{});scores=review.get('scores',{})
        human=bool(review.get('reviewer') and review.get('notes') and all(type(scores.get(k)) is int and scores[k]==2 for k in corpus['rubric']))
        rows.append({'id':case['id'],'automatic_failures':checks,'status':'failed' if checks else 'passed' if human else 'human_review_pending'})
    known={c['id'] for c in corpus['cases']};unknown=sorted(set(outputs)-known)
    return {'version':corpus['version'],'cases':rows,'unknown_output_ids':unknown,'passed':not unknown and all(r['status']=='passed' for r in rows),'note':'Automatic invariants do not establish semantic quality; references need native-speaker review.'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--outputs',type=Path,required=True);p.add_argument('--reviews',type=Path);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    corpus=json.loads((ROOT/'backend/data/quality/five-language-v1.json').read_text());report=evaluate(corpus,json.loads(a.outputs.read_text()),json.loads(a.reviews.read_text()) if a.reviews else None);a.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'cases':len(report['cases'])}));raise SystemExit(0 if report['passed'] else 1)
