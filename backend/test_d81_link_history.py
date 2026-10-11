"""Explicit real-book link/reducer regression, without paid translation.

Replays original text through actual single/bilingual reduce/package. Deliberate
href corruption exercises deterministic restoration; separate final-artifact
mutations exercise the graph gate. Not a semantic translation evaluation.
"""
import argparse
import copy
from collections import Counter
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from bs4 import BeautifulSoup


def require(value, message):
    if not value: raise AssertionError(message)


def fingerprint(path):
    return path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--book', nargs=2, action='append', required=True)
    args = parser.parse_args()
    with ExitStack() as stack:
        stack.enter_context(patch('dotenv.load_dotenv', return_value=False))
        guards = [stack.enter_context(patch(name, side_effect=AssertionError('External call forbidden')))
                  for name in ('socket.socket.connect', 'socket.getaddrinfo', 'socket.socket.sendto',
                               'openai.OpenAI', 'openai.AsyncOpenAI', 'smtplib.SMTP', 'smtplib.SMTP_SSL')]
        from app.engine.compiler import ExtremeCompiler
        from app.engine.epub_validation import validate_epub
        from app.engine.unpacker import EpubUnpacker
        from app.domain.manifest_service import build_manifest, chapter_requires_translation
        from app.domain.chapter_reduce_service import apply_chunk_results
        from app.domain.book_reduce_service import reduce_and_package
        from app.domain.epub_link_audit import audit_epub_links

        def audit(path):
            with zipfile.ZipFile(path) as z: return audit_epub_links(z)

        def healthy(report):
            return not any(report[k] for k in ('links_broken', 'links_unsafe', 'duplicate_anchors', 'nested_links'))

        for configured, expected_sha in args.book:
            source = Path(configured).resolve(strict=True)
            before = fingerprint(source)
            require(before[2] == expected_sha, 'Explicit original SHA mismatch')
            with tempfile.TemporaryDirectory(prefix='link-history-') as tmp:
                root = Path(tmp); prepared = root / 'prepared.epub'
                with redirect_stdout(io.StringIO()):
                    compiler = ExtremeCompiler(str(source), str(prepared), output_mode='keep', enable_translation=False)
                    require(compiler.run() and compiler.validation_passed, 'Real preprocessing / EPUBCheck failed')
                    manifest = build_manifest(str(prepared), 'link-history')
                    book = EpubUnpacker(prepared).load_book()
                require(not manifest.get('error') and book is not None, 'Real manifest failed')
                require(healthy(audit(prepared)), 'Prepared book links invalid')
                items = {item.get_name(): item for item in book.get_items()}
                results = []
                for bilingual in (False, True):
                    output = root / ('bilingual.epub' if bilingual else 'single.epub')
                    contents = {}; restored = chunks = 0
                    for chapter in manifest['chapters']:
                        if not chapter_requires_translation(chapter): continue
                        rows = []
                        for chunk in chapter.get('chunks', []):
                            raw = chunk['html']; soup = BeautifulSoup(raw, 'html.parser')
                            link = soup.find('a', href=True)
                            if link is not None:
                                link['href'] = '#deliberately-corrupted-test-target'
                                raw = str(soup); restored += 1
                            rows.append(SimpleNamespace(**{**chunk, 'translated_html': raw}))
                        name = chapter['file_path']; chunks += len(rows)
                        original = items[name].get_content()
                        contents[name] = apply_chunk_results(original, rows, bilingual)
                        rewritten = BeautifulSoup(contents[name], 'html.parser')
                        if bilingual:
                            for retained in rewritten.select('.epub-original'):
                                translated = retained.find_next_sibling(class_='epub-translated')
                                require(translated is not None, 'Missing bilingual output')
                                require([a.get('href') for a in retained.find_all('a')] ==
                                        [a.get('href') for a in translated.find_all('a')],
                                        'Translated links differ from their exact original targets')
                        else:
                            def links(soup):
                                return Counter((a.get('href'), a.get('id'), a.get('name')) for a in soup.find_all('a'))
                            require(links(BeautifulSoup(original, 'html.parser')) == links(rewritten),
                                    'Single-language link identities changed')
                    with redirect_stdout(io.StringIO()):
                        require(reduce_and_package(str(prepared), str(output), contents.get), 'Real reduce/package failed')
                    validation = validate_epub(output, os.environ['EPUBCHECK_JAR'])
                    require(validation.passed, validation.message)
                    report = audit(output)
                    require(healthy(report), 'Real output link graph failed: ' + json.dumps(report))
                    results.append({'bilingual': bilingual, 'chunks_replayed': chunks, 'corrupted_hrefs_restored': restored,
                                    'links_checked': report['links_checked'], 'epubcheck_passed': True})

                mutations = []
                with zipfile.ZipFile(prepared) as z:
                    candidates = []
                    for name in z.namelist():
                        if name.endswith(('.html', '.xhtml')):
                            soup = BeautifulSoup(z.read(name), 'html.parser')
                            if soup.find('a', href=True): candidates.append((name, soup))
                    require(candidates, 'No real link to mutate')
                    name, soup = candidates[0]
                    for kind in ('bad_target', 'duplicate_anchor'):
                        mutated = copy.deepcopy(soup)
                        if kind == 'bad_target': mutated.find('a', href=True)['href'] = '#missing-test-anchor'
                        else:
                            anchor = mutated.find('a', href=True)
                            anchor['id'] = 'duplicate-test-anchor'
                            anchor.insert_after(copy.deepcopy(anchor))
                        target = root / (kind + '.epub')
                        with zipfile.ZipFile(target, 'w') as changed:
                            for info in z.infolist():
                                changed.writestr(copy.copy(info), str(mutated).encode() if info.filename == name else z.read(info.filename))
                        require(not healthy(audit(target)), 'Real mutation escaped graph gate')
                        mutations.append(kind)
                require(fingerprint(source) == before, 'Original changed')
                print(json.dumps({'source_sha256': expected_sha, 'variants': results, 'mutations_blocked': mutations,
                                  'source_unchanged': True}, sort_keys=True), flush=True)
        for guard in guards: guard.assert_not_called()


if __name__ == '__main__': main()
