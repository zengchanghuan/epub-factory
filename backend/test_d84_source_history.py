"""Explicit SHA-pinned source compatibility regression; no paid calls.

Runs actual non-LLM conversion and EPUBCheck. Independently compares source
prose (allowing existing punctuation normalization), links, IDs, image bytes.
No original or historical translation is overwritten.
"""
import argparse
from collections import Counter
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import posixpath
import re
import shutil
import tempfile
from urllib.parse import unquote, urlsplit
from unittest.mock import patch
import zipfile

from lxml import etree


def identity(path):
    return path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def xml(raw):
    return etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))


def members(archive):
    container = xml(archive.read('META-INF/container.xml'))
    opf = container.find('.//{*}rootfile').get('full-path')
    package = xml(archive.read(opf))
    return {n.get('href'): (n.get('media-type'), posixpath.normpath(posixpath.join(
        posixpath.dirname(opf), unquote(n.get('href'))))) for n in package.findall('{*}manifest/{*}item')}


def normalized_text(root):
    body = root.find('{*}body')
    value = ''.join(body.itertext()) if body is not None else ''
    # Existing typography conversion, not a new content-repair allowance.
    value = re.sub(r'\.{3,}', '…', value)
    value = re.sub(r'(?<![:/\-])\-{2}(?![\-/>])', '—', value)
    return ''.join(value.split())


def navigation(archive, manifest):
    paths = {path: href for href, (_kind, path) in manifest.items()}
    result = []
    for _href, (kind, path) in manifest.items():
        if kind != 'application/x-dtbncx+xml': continue
        entries = []
        for node in xml(archive.read(path)).findall('.//{*}navPoint'):
            label = node.find('{*}navLabel/{*}text')
            link = urlsplit(node.find('{*}content').get('src'))
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(link.path)))
            depth = sum(etree.QName(n).localname == 'navPoint' for n in node.iterancestors())
            entries.append((' '.join(''.join(label.itertext()).split()), paths[target], unquote(link.fragment), depth))
        result.append(entries)
    return result


def verify(source, output):
    from app.domain.epub_link_audit import audit_epub_links
    from app.domain.content_completeness import capture_content_baseline, audit_content_completeness
    result = {'documents': 0, 'images': 0, 'links_preserved': 0, 'ids_preserved': 0,
              'backlink_labels': 0, 'metadata_values_preserved': 0}
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as artifact:
        old, new = members(original), members(artifact)
        assert old.keys() <= new.keys(), 'Original manifest resources lost'
        before_navigation, after_navigation = navigation(original, old), navigation(artifact, new)
        assert before_navigation == after_navigation, 'TOC labels, order, nesting or destinations changed'
        result['toc_entries_preserved'] = sum(len(entries) for entries in before_navigation)
        for href, (kind, path) in old.items():
            raw, rendered = original.read(path), artifact.read(new[href][1])
            if kind.startswith('image/'):
                assert raw == rendered, 'Image bytes changed: ' + href
                result['images'] += 1
            if kind not in {'application/xhtml+xml', 'text/html'}:
                continue
            before, after = xml(raw), xml(rendered)
            # Only newly generated navigation labels may add text.
            markers = after.xpath('//*[@data-epub-compat-backlink-label]')
            assert all(n.text == '↩' and n.getparent().get('href') for n in markers)
            result['backlink_labels'] += len(markers)
            for node in markers: node.text = ''
            assert normalized_text(before) == normalized_text(after), 'Prose changed: ' + href
            source_links = Counter(before.xpath('//*[local-name()="a"]/@href'))
            output_links = Counter(after.xpath('//*[local-name()="a"]/@href'))
            assert source_links == output_links, 'Link target multiplicity changed: ' + href
            ids = before.xpath('//@id')
            assert set(ids) <= set(after.xpath('//@id')), 'Source anchor lost: ' + href
            assert not after.xpath('//*[local-name()="a"]//*[local-name()="a"]')
            for node in before.xpath('//*[local-name()="head"]/*[local-name()="meta"]'):
                if set(node.attrib) == {'content'}:
                    assert after.xpath('//*[local-name()="meta"][@name="epub-factory-legacy-content"][@content=$v]',
                                       v=node.get('content'))
                    result['metadata_values_preserved'] += 1
            result['documents'] += 1
            result['links_preserved'] += sum(source_links.values())
            result['ids_preserved'] += len(ids)
        audit = audit_epub_links(artifact)
        assert not any(audit[k] for k in ('links_broken', 'links_unsafe', 'duplicate_anchors', 'nested_links')), audit
        result['final_links_checked'] = audit['links_checked']
    coverage = audit_content_completeness(capture_content_baseline(source), output)
    assert coverage['status'] == 'passed', coverage
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--book', nargs=2, action='append', required=True, metavar=('PATH', 'SHA256'))
    parser.add_argument('--output-dir', type=Path, help='Explicit new artifacts only; never overwrite')
    args = parser.parse_args()
    with ExitStack() as stack:
        stack.enter_context(patch('dotenv.load_dotenv', return_value=False))
        guards = [stack.enter_context(patch(name, side_effect=AssertionError('External calls forbidden')))
                  for name in ('socket.socket.connect', 'socket.getaddrinfo', 'socket.socket.sendto',
                               'openai.OpenAI', 'openai.AsyncOpenAI', 'smtplib.SMTP', 'smtplib.SMTP_SSL')]
        from app.engine.compiler import ExtremeCompiler
        from app.domain.epub_input_integrity import validate_epub_resources
        for filename, expected in args.book:
            source = Path(filename).resolve(strict=True); before = identity(source)
            assert before[2] == expected, 'Explicit source SHA mismatch'
            with source.open('rb') as stream: validate_epub_resources(stream)
            with tempfile.TemporaryDirectory(prefix='source-compat-history-') as tmp:
                output = Path(tmp) / 'converted.epub'
                captured = io.StringIO()
                with redirect_stdout(captured):
                    compiler = ExtremeCompiler(str(source), str(output), output_mode='keep', enable_translation=False)
                    ok = compiler.run()
                    assert ok and compiler.validation_passed and compiler.metrics.mode == 'full', captured.getvalue()[-5000:]
                    result = verify(source, output)
                result.update(source_sha256=expected, epubcheck_passed=True, real_model_calls=0)
                assert identity(source) == before, 'Original modified'
                for guard in guards: guard.assert_not_called()
                if args.output_dir:
                    args.output_dir.mkdir(parents=True, exist_ok=True)
                    target = args.output_dir / (source.stem + '_兼容修正版.epub')
                    with target.open('xb') as destination, output.open('rb') as stream:
                        shutil.copyfileobj(stream, destination)
                    result['artifact_sha256'] = identity(target)[2]
                print(json.dumps(result, ensure_ascii=False), flush=True)
        for guard in guards: guard.assert_not_called()


if __name__ == '__main__':
    main()
