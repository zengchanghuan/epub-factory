#!/usr/bin/env python3
"""Replay private PDF acceptance corpus through current code, without network or keys.
Expected inputs: manifest.json, samples/, real-evaluation/{01,02,03}/ saved responses.
Writes a new isolated run directory; never modifies the reference responses.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from unittest.mock import patch
import zipfile
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.domain.pdf import content, local, pipeline, state, providers


def load(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    root = args.corpus_root.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix='pdf-regression-', dir=args.output_dir.resolve()))
    os.environ.update(PDF_WORK_DIR=str(run), MINERU_API_KEY='offline-never-send',
                      MINERU_LOCAL_PRIORITY_PAGES='800', PDF_DEEPSEEK_API_KEY='offline-never-send')
    mapping = {}
    samples = load(root / 'manifest.json')['samples']
    for sample in samples[:3]:
        key = sample['file'][:2]
        for page, origin in enumerate(sample['source_pages'], 1):
            mapping[(origin['file'], origin['physical_page'])] = (key, page)
    summaries = []
    for sample in samples:
        for mode in (['auto', 'flash'] if sample['file'].startswith('03') else ['auto']):
            key = sample['file'][:2]
            source = (root / 'samples' / sample['file']).resolve()
            if source.parent != root / 'samples':
                raise ValueError('Invalid sample path')
            job = 'sample-' + key + '-' + mode
            directory = state.job_dir(job)
            info = local.inspect_pdf(source)
            state.save(directory / 'source.json', info)
            candidates = {}
            for number, origin in enumerate(sample['source_pages'], 1):
                original, page = mapping[(origin['file'], origin['physical_page'])]
                ref = root / 'real-evaluation' / original
                shutil.copy2(ref / f'page-{page}.jpg', directory / f'page-{number}.jpg')
                shutil.copy2(ref / f'flash-page-{page}' / f'flash-{page}-full.json', directory / f'flash-{number}-full.json')
                parsed = content.mineru_pages(load(ref / 'mineru-raw-content.json'), 6)
                candidates[str(number)] = copy.deepcopy(parsed[str(page)])
            if mode == 'flash':
                state.save(directory / 'route.json', {'provider': 'deepseek-flash'})
            class CachedMinerU:
                def __init__(self, *args): pass
                def extract(self, *args): return copy.deepcopy(candidates)
            with patch.object(pipeline, 'MinerU', CachedMinerU), patch.object(providers.Flash, 'call', side_effect=AssertionError('OCR network forbidden')), patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')):
                artifact, summary = pipeline.prepare(source, job, {'source_sha256': info['sha256']})
            with zipfile.ZipFile(artifact) as book:
                for name in book.namelist():
                    if name.endswith(('.xhtml', '.opf', '.xml')):
                        ET.fromstring(book.read(name))
                images = [n for n in book.namelist() if n.startswith('OEBPS/images/')]
                assert len(images) == info['page_count']
                if key == '01':
                    # Real regression: code was formerly one giant prose block.
                    page4 = load(directory / 'page-4.json')
                    assert any(b['type'] == 'code' and 'type(2)' in b['text'] for b in page4['blocks'])
                    assert len(page4['blocks']) > 5
                if key == '03' and mode == 'auto':
                    assert b'pdf-equation' in book.read('OEBPS/page-0006.xhtml')
                    assert info['pages'][5]['route'] == 'ocr'
            if mode == 'flash':
                assert summary['pending_pages'] == [{'page': 4, 'reason': 'missing_confidence_field'}]
                assert (directory / 'page-6.json').exists(), 'Later pages must continue'
            else:
                assert not summary['review_required']
            row = {'sample': key, 'mode': mode, 'routes': [p['route'] for p in info['pages']],
                   'summary': summary, 'epub': str(artifact), 'paid_calls': 0}
            summaries.append(row)
            print(json.dumps(row, ensure_ascii=False))
    state.save(run / 'summary.json', summaries)
    print('Report: ' + str(run / 'summary.json'))


if __name__ == '__main__':
    main()
