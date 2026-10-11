#!/usr/bin/env python3
"""Compare two numeric auditors on real paired EPUBs, without changing books/orders.

The private corpus JSON supplies source/target paths, SHA256s and optional glossary
path/key. No title, job ID or passage is built into the checker. Reports contain
counts/hashes only; source text stays in memory. This is structural/rule regression,
not a claim that translations are semantically correct.
"""
import argparse
from collections import Counter
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unicodedata
import zipfile


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def identity(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()]


def corruption_checks(module, source, target):
    """Remove all target representations of each shared fact; require its warning.

    Check the *selected key*, not just any warning already present in a paragraph.
    This does not pretend a bag of facts proves their sentence-level attribution.
    """
    source = unicodedata.normalize('NFKC', source)
    target = unicodedata.normalize('NFKC', target)
    source_facts = module.numeric_facts(source)
    left = Counter((f.value, f.kind) for f in source_facts)
    facts = module.numeric_facts(target)
    right = Counter((f.value, f.kind) for f in facts)
    checks = Counter()
    for key in sorted(left.keys() & right.keys()):
        selected = [f for f in facts if (f.value, f.kind) == key]
        bad = target
        for fact in reversed(selected):
            bad = bad[:fact.start] + ' [removed numeric fact] ' + bad[fact.end:]
        selected_source_raw = {f.raw for f in source_facts if (f.value, f.kind) == key}
        def missing_count(text):
            # A range endpoint raw token can omit an inherited currency/scale;
            # retain the original source fact association instead of reparsing it.
            return sum(raw in selected_source_raw for raw in module.missing_numeric_facts(source, text))
        assert missing_count(bad) > missing_count(target), 'selected numeric corruption not detected'
        checks[key[1]] += 1
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    def offline(event, _args):
        if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind'}:
            raise RuntimeError('numeric regression forbids network access')
    sys.addaudithook(offline)
    sys.path.insert(0, str(args.project / 'backend'))
    from bs4 import BeautifulSoup
    from app.domain.manifest_service import build_manifest, chapter_requires_translation
    from app.domain.chapter_reduce_service import get_node_by_locator
    import app.domain.translation_quality_audit as qa
    baseline = load_module('numeric_history_baseline', args.baseline)
    candidate = load_module('numeric_history_candidate', args.candidate)
    cases = json.loads(args.corpus.read_text())
    protected = {p: identity(p) for p in [args.baseline, args.candidate, args.corpus]}
    result = {'status': 'running', 'model_calls': 0, 'cases': [],
              'baseline_sha256': protected[args.baseline][2],
              'candidate_sha256': protected[args.candidate][2]}
    old_numeric = qa.missing_numeric_facts
    try:
        for case in cases:
            paths = [Path(case[k]) for k in ('source', 'target')]
            for key, path in zip(('source', 'target'), paths):
                protected[path] = identity(path)
                assert protected[path][2] == case[key + '_sha256'], 'corpus identity mismatch'
            glossary = {}
            if case.get('glossary'):
                path = Path(case['glossary']); protected[path] = identity(path)
                glossary = json.loads(path.read_text())
                if case.get('glossary_key'):
                    glossary = glossary[case['glossary_key']]
            counts = {'baseline': Counter(), 'candidate': Counter()}
            mutations = Counter(); changed = added = 0
            new_warning_details = []
            with (args.evidence / f'book-{len(result["cases"])}.log').open('x') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                manifest = build_manifest(str(paths[0]), 'offline-numeric-history')
                assert not manifest.get('error'), 'source manifest failed'
                with zipfile.ZipFile(paths[1]) as archive:
                    for chapter in manifest['chapters']:
                        if not chapter_requires_translation(chapter): continue
                        names = [n for n in archive.namelist() if n == chapter['file_path'] or n.endswith('/' + chapter['file_path'])]
                        assert len(names) == 1, 'ambiguous target document'
                        soup = BeautifulSoup(archive.read(names[0]), 'html.parser')
                        for chunk in chapter['chunks']:
                            node = get_node_by_locator(soup, chunk['locator'])
                            original = BeautifulSoup(chunk['html'], 'html.parser').find()
                            assert node is not None and original is not None
                            assert node.name == original.name and node.get('id') == original.get('id'), 'alignment failed'
                            audits = []
                            for label, module in [('baseline', baseline), ('candidate', candidate)]:
                                qa.missing_numeric_facts = module.missing_numeric_facts
                                audit = qa.audit_translation_chunk(original_html=chunk['html'], translated_html=str(node), glossary=glossary)
                                audits.append(audit)
                                counts[label].update(['paired', audit.risk_level, *audit.flags])
                            a, b = audits
                            assert set(a.flags)-{'numbers_missing'} == set(b.flags)-{'numbers_missing'}, 'non-numeric QA changed'
                            changed += a.numbers_missing != b.numbers_missing
                            added += not a.numbers_missing and bool(b.numbers_missing)
                            if not a.numbers_missing and b.numbers_missing:
                                new_warning_details.append({
                                    'chunk_id': chunk['chunk_id'], 'missing_numeric_tokens': b.numbers_missing,
                                    'target_temporal_facts': [dict(value=str(f.value), kind=f.kind)
                                        for f in candidate.numeric_facts(qa._numeric_text(str(node)))
                                        if f.kind.startswith(('decade:', 'century:'))]})
                            mutations.update(corruption_checks(candidate, qa._numeric_text(chunk['html']), qa._numeric_text(str(node))))
                assert counts['baseline']['paired'] == counts['candidate']['paired'] == case['expected_chunks']
                assert counts['candidate']['fail'] == counts['baseline']['fail'] == 0
                assert sum(mutations.values()) > 0, 'no real numeric negative coverage'
            for path, before in protected.items(): assert identity(path) == before, 'protected input changed'
            record = {'source_sha256': case['source_sha256'], 'target_sha256': case['target_sha256'],
                      'counts': counts, 'changed_numeric_blocks': changed, 'new_numeric_warning_blocks': added,
                      'new_warning_details': new_warning_details,
                      'detected_real_numeric_corruptions': dict(mutations), 'inputs_unchanged': True,
                      'other_flags_unchanged': True}
            result['cases'].append(record)
            (args.evidence / 'report.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(record), flush=True)
        result['status'] = 'passed'
    except BaseException:
        result['status'] = 'failed'
        raise
    finally:
        qa.missing_numeric_facts = old_numeric
        (args.evidence / 'report.json').write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
