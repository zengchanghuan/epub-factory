#!/usr/bin/env python3
"""Offline source/target review. Never calls a model or changes an EPUB/order."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import zipfile
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.domain.manifest_service import build_manifest
from app.domain.chapter_reduce_service import get_node_by_locator
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_qa_service import audit_translated_epub_output, QA_RULES_VERSION
from app.domain.translation_residual_policy import confirmed_preserved_terms


def audit_book(source, target, glossary=None, *, include_chunks=False):
    preserved_terms = confirmed_preserved_terms(glossary)
    manifest = build_manifest(str(source), 'offline-audit')
    if manifest.get('error'):
        raise ValueError(manifest['error'])
    counts = Counter()
    findings, alignment_failures, chunk_audits = [], [], []
    expected = sum(len(c['chunks']) for c in manifest['chapters'] if c['chapter_kind'] == 'body')
    with zipfile.ZipFile(target) as archive:
        for chapter in manifest['chapters']:
            if chapter['chapter_kind'] != 'body':
                continue
            file_path = chapter['file_path']
            # ebooklib paths are OPF-relative, not necessarily ZIP-root-relative.
            candidates = [n for n in archive.namelist() if n == file_path or n.endswith('/' + file_path)]
            if len(candidates) != 1:
                alignment_failures.append({'file': file_path, 'reason': 'ambiguous_or_missing_document'})
                continue
            soup = BeautifulSoup(archive.read(candidates[0]), 'html.parser')
            for chunk in chapter['chunks']:
                node = get_node_by_locator(soup, chunk['locator'])
                original = BeautifulSoup(chunk['html'], 'html.parser').find()
                if node is None or (original and (node.name != original.name or original.get('id') != node.get('id'))):
                    alignment_failures.append({'file': file_path, 'chunk_id': chunk['chunk_id'], 'reason': 'locator_mismatch'})
                    continue
                audit = audit_translation_chunk(original_html=chunk['html'], translated_html=str(node), glossary=glossary,
                                                preserved_terms=preserved_terms).to_dict()
                counts['paired_chunks'] += 1
                counts[audit['risk_level']] += 1
                counts.update(audit['flags'])
                if include_chunks:
                    chunk_audits.append({'chunk_id': chunk['chunk_id'], 'chapter_id': chapter['chapter_id'],
                                         'file_path': file_path, **audit})
                if audit['flags']:
                    findings.append({'file': file_path, 'chunk_id': chunk['chunk_id'], 'locator': chunk['locator'], **audit})
    report = {'rules_version': QA_RULES_VERSION, 'mode': 'offline; no LLM calls; warnings are not confirmed errors',
            'source_sha256': hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            'target_sha256': hashlib.sha256(Path(target).read_bytes()).hexdigest(),
            'counts': dict(counts), 'expected_paired_chunks': expected, 'alignment_failures': alignment_failures,
            'confirmed_preserved_terms': preserved_terms,
            'artifact_audit': audit_translated_epub_output(target, sample_limit=100, preserved_terms=preserved_terms), 'findings': findings}
    if include_chunks:
        report['chunk_audits'] = chunk_audits
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('target', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--preserve-term', action='append', default=[],
                        help='Exact term explicitly confirmed by the user; repeatable, book-scoped')
    args = parser.parse_args()
    if args.report.resolve() in {args.source.resolve(), args.target.resolve()}:
        parser.error('Report must not overwrite an EPUB')
    report = audit_book(args.source, args.target, {term: term for term in args.preserve_term})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'findings'}, ensure_ascii=False, indent=2))
    if report['alignment_failures']:
        raise SystemExit(1)
