"""Explicit real-book gate; no credentials, model, payment or production state.

Run separately: python test_d80_content_history.py --book /path/book.epub SHA256
Repeat --book for additional originals. Only temporary candidate EPUBs are
written; originals and their SHA/mtime must remain unchanged. This is coverage
regression with retained original text, NOT paid-model semantic evaluation.
"""
import argparse
import copy
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import posixpath
import tempfile
import time
from unittest.mock import patch
import zipfile

from lxml import etree


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--book', nargs=2, action='append', required=True, metavar=('PATH', 'SHA256'))
    args = parser.parse_args()
    with ExitStack() as stack:
        stack.enter_context(patch('dotenv.load_dotenv', return_value=False))
        guards = [stack.enter_context(patch(name, side_effect=AssertionError('External call forbidden')))
                  for name in ('socket.socket.connect', 'socket.getaddrinfo', 'socket.socket.sendto',
                               'openai.OpenAI', 'openai.AsyncOpenAI', 'smtplib.SMTP', 'smtplib.SMTP_SSL')]
        from app.domain.content_completeness import capture_content_baseline, audit_content_completeness
        from app.engine.compiler import ExtremeCompiler
        from app.engine.epub_compat import package_path

        for index, (configured, expected) in enumerate(args.book, 1):
            started = time.monotonic()
            source = Path(configured).resolve(strict=True)
            before = (source.stat().st_size, source.stat().st_mtime_ns, sha(source))
            require(before[2] == expected, 'Explicit original SHA mismatch')
            with tempfile.TemporaryDirectory(prefix='content-real-regression-') as tmp:
                root = Path(tmp)
                output = root / 'converted.epub'
                with redirect_stdout(io.StringIO()):
                    baseline = capture_content_baseline(source)
                    compiler = ExtremeCompiler(str(source), str(output), output_mode='keep', enable_translation=False)
                    require(compiler.run() and compiler.validation_passed and compiler.metrics.mode == 'full',
                            'Real normal conversion / EPUBCheck did not pass')
                normal = audit_content_completeness(baseline, output)
                require(normal['status'] == 'passed', 'Real normal coverage failed: ' + json.dumps(normal, ensure_ascii=True))
                with zipfile.ZipFile(output) as z:
                    opf = package_path(z)
                    # Choose an expected content document with an actual text
                    # paragraph. Mutation targets come from this real artifact.
                    selected = None
                    for name in baseline['documents']:
                        member = posixpath.normpath(posixpath.join(posixpath.dirname(opf), name))
                        raw = z.read(member)
                        tree = etree.fromstring(raw)
                        candidates = tree.xpath('//*[local-name()="p" or local-name()="h1" or local-name()="h2"]')
                        candidates = [n for n in candidates if ''.join(n.itertext()).strip()]
                        if candidates:
                            selected = member, raw
                            break
                    require(selected is not None, 'No real text block to mutate')
                    member, raw = selected
                    results = {}
                    for mutation in ('delete_document', 'delete_block', 'empty_block'):
                        target = root / (mutation + '.epub')
                        tree = etree.fromstring(raw)
                        node = next(n for n in tree.xpath('//*[local-name()="p" or local-name()="h1" or local-name()="h2"]')
                                    if ''.join(n.itertext()).strip())
                        if mutation == 'delete_block':
                            node.getparent().remove(node)
                        elif mutation == 'empty_block':
                            node.text = None
                            for child in list(node): node.remove(child)
                        with zipfile.ZipFile(target, 'w') as changed:
                            for info in z.infolist():
                                if info.filename == member:
                                    if mutation == 'delete_document': continue
                                    value = etree.tostring(tree, encoding='utf-8', xml_declaration=True)
                                else:
                                    value = z.read(info.filename)
                                changed.writestr(copy.copy(info), value)
                        report = audit_content_completeness(baseline, target)
                        require(report['status'] == 'failed', 'Real mutation escaped coverage: ' + mutation)
                        results[mutation] = report['missing_units'] + report['empty_units']
                require((source.stat().st_size, source.stat().st_mtime_ns, sha(source)) == before, 'Original changed')
                print(json.dumps({'book_index': index, 'source_sha256': expected, 'normal_status': normal['status'],
                    'expected_documents': normal['expected_documents'], 'expected_units': normal['expected_units'],
                    'negative_cases_blocked': results, 'source_unchanged': True, 'epubcheck_passed': True,
                    'seconds': round(time.monotonic()-started, 3)}, sort_keys=True), flush=True)
        for guard in guards: guard.assert_not_called()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
