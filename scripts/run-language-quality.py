"""Opt-in real-model synthetic benchmark. Uses production translators, isolated state.

Credentials must be supplied in the environment. Never loads .env or customer data.
This does not certify native-language quality or test a complete paid book workflow.
"""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from html import escape

ROOT = Path(__file__).resolve().parents[1]


def extract_polish_text(content):
    from lxml import etree
    return ''.join(etree.fromstring(content).itertext())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--case', action='append', dest='case_ids', help='Run only named synthetic case IDs')
    args = parser.parse_args()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    # Relative caches, SQLite accounting and optional distributed state are isolated.
    os.chdir(out)
    os.environ['DATABASE_URL'] = 'sqlite:///' + str(out / 'usage.sqlite')
    os.environ['EPUB_LLM_RATE_LIMITER_ENABLED'] = '0'
    os.environ['EPUB_LLM_GLOBAL_HEALTH_ENABLED'] = '0'
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.engine.cleaners.semantics_translator import SemanticsTranslator
    from app.engine.cleaners.llm_polish import LLMPolisher, plan_document
    from app.infra.llm_usage_ledger import usage_scope
    from app.infra.llm_gateway import dispatch_budget_scope
    from lxml import html

    corpus = json.loads((ROOT / 'backend/data/quality/five-language-v1.json').read_text())
    if args.case_ids:
        known = {case['id'] for case in corpus['cases']}
        if set(args.case_ids) - known:
            raise ValueError('Unknown synthetic case ID')
        corpus['cases'] = [case for case in corpus['cases'] if case['id'] in args.case_ids]
    (out / 'corpus.json').write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + '\n')
    outputs, evidence = {}, []
    calls = 0

    def reserve(*unused, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 80:
            raise RuntimeError('Synthetic evaluation request budget exhausted')

    for case in corpus['cases']:
        row = {'id': case['id'], 'task': case['task']}
        with usage_scope('quality-' + case['id'], 'synthetic-v1') as ledger, dispatch_budget_scope(reserve):
            try:
                if case['task'] == 'translation':
                    worker = SemanticsTranslator(target_lang=case['target_language'], concurrency=1, cache_policy='fresh')
                    worker.source_lang = case['source_language']
                    result = asyncio.run(worker.translate_single_chunk_async('<p>' + escape(case['input']) + '</p>'))
                    row['model'] = worker.model
                    if result.error:
                        raise RuntimeError('Translation result failed')
                    outputs[case['id']] = html.fromstring('<div>' + result.translated_html + '</div>').text_content()
                else:
                    source = ('<html xmlns="http://www.w3.org/1999/xhtml"><body><p>' + escape(case['input']) + '</p></body></html>').encode()
                    plan = plan_document(source, language=case['target_language'])
                    with LLMPolisher() as worker:
                        result = worker.polish_document(plan)
                        row['model'] = worker.model
                        outputs[case['id']] = extract_polish_text(result)
                        row['stats'] = worker.stats.to_dict()
                row['status'] = 'completed'
            except Exception as exc:
                # Never serialize provider errors, credentials or URLs.
                row.update(status='failed', error_type=type(exc).__name__)
            row['billing'] = ledger.summary('quality-' + case['id'])
        evidence.append(row)
        (out / 'outputs.json').write_text(json.dumps(outputs, ensure_ascii=False, indent=2) + '\n')
        (out / 'execution.json').write_text(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), 'cases': evidence}, ensure_ascii=False, indent=2) + '\n')
        print(case['id'], row['status'], flush=True)
    spec = importlib.util.spec_from_file_location('quality', ROOT / 'scripts/evaluate-language-quality.py')
    scoring = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scoring)
    report = scoring.evaluate(corpus, outputs)
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return int(any(row['status'] == 'failed' for row in evidence) or any(row['automatic_failures'] for row in report['cases']))


if __name__ == '__main__':
    raise SystemExit(main())
