"""Explicit real historical empty-paragraph regression; no real model calls.

Uses matched source/damaged EPUBs and injected empty responses. This proves
failure handling and source retention, not translation quality or book repair.
"""
import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import posixpath
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote
import zipfile

from bs4 import BeautifulSoup, NavigableString
from lxml import etree


def fingerprint(path):
    return (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


def documents(path):
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with zipfile.ZipFile(path) as z:
        container = etree.fromstring(z.read('META-INF/container.xml'), parser)
        opf = container.xpath('//*[local-name()="rootfile"]/@full-path')[0]
        manifest = etree.fromstring(z.read(opf), parser)
        result = {}
        for item in manifest.xpath('//*[local-name()="manifest"]/*[local-name()="item"]'):
            if item.get('media-type') not in ('application/xhtml+xml', 'text/html'): continue
            relative = unquote(item.get('href'))
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf), relative))
            result[relative] = z.read(name)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', nargs=2, required=True, metavar=('PATH', 'SHA'))
    parser.add_argument('--damaged', nargs=2, required=True, metavar=('PATH', 'SHA'))
    parser.add_argument('--expected-missing', type=int, required=True)
    args = parser.parse_args()
    source, damaged = (Path(v[0]).resolve(strict=True) for v in (args.source, args.damaged))
    before = [fingerprint(p) for p in (source, damaged)]
    assert [r[2] for r in before] == [args.source[1], args.damaged[1]]
    originals, outputs = documents(source), documents(damaged)
    assert originals.keys() == outputs.keys()
    missing = []
    for name, raw in originals.items():
        a, b = [BeautifulSoup(doc, 'html.parser').find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
                for doc in (raw, outputs[name])]
        assert len(a) == len(b)
        for original, translated in zip(a, b):
            assert original.name == translated.name
            if original.get_text(strip=True) and not translated.get_text(strip=True):
                missing.append(str(original))
    assert len(missing) == args.expected_missing
    with tempfile.TemporaryDirectory(prefix='empty-history-') as tmp, ExitStack() as stack:
        stack.enter_context(patch('dotenv.load_dotenv', return_value=False))
        guards = [stack.enter_context(patch(n, side_effect=AssertionError('external request forbidden')))
                  for n in ('socket.socket.connect', 'socket.getaddrinfo', 'socket.socket.sendto',
                            'openai.OpenAI', 'openai.AsyncOpenAI', 'smtplib.SMTP', 'smtplib.SMTP_SSL')]
        stack.enter_context(patch.dict(os.environ, {
            'EPUB_TRANSLATION_QUALITY_RETRIES': '1', 'EPUB_TRANSLATION_CHUNK_RETRY_BUDGET': '1',
            'EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE': '0', 'OPENAI_MAX_RETRIES': '1',
            'EPUB_TRANSLATION_PROACTIVE_QUALITY_MODEL_ENABLED': '0',
        }))
        from app.engine.cleaners.semantics_translator import SemanticsTranslator
        from app.engine.translation_cache import TranslationCache
        from app.domain.chapter_reduce_service import apply_chunk_results
        from app.engine.chunk_extractor import is_external_text_node
        cache = TranslationCache(str(Path(tmp) / 'cache.db'))
        stack.enter_context(patch('app.engine.cleaners.semantics_translator.TranslationCache', return_value=cache))
        reports = []
        for variant in ('whitespace', 'empty_markup'):
            translator = SemanticsTranslator(model='deepseek-flash', quality_fallback_model='')
            calls = []
            async def request(payload, **kwargs):
                calls.append(len(payload))
                result = {}
                for item in payload:
                    soup = BeautifulSoup(item['html'], 'html.parser')
                    for node in list(soup.find_all(string=True)):
                        if is_external_text_node(node): node.replace_with(NavigableString(' \n '))
                    result[item['id']] = ' \n\t ' if variant == 'whitespace' else str(soup)
                return result, {'attempts': 1}
            translator._call_llm_json_batch = request
            with patch.object(cache, 'set') as writes:
                results = asyncio.run(translator.translate_many_chunks_async(missing))
                assert len(results) == len(missing)
                assert all(r.error for r in results)
                assert translator.stats.translated_chunks == 0
                assert translator.stats.failed_chunks == len(missing)
                writes.assert_not_called()
            for raw, result in zip(missing, results):
                block = BeautifulSoup(raw, 'html.parser').find()
                rewritten = apply_chunk_results(('<html><body>' + raw + '</body></html>').encode(),
                    [SimpleNamespace(locator='/html/body/' + block.name, translated_html=result.translated_html,
                                     sequence=0, chunk_id='history')], False)
                restored = BeautifulSoup(rewritten, 'html.parser').body
                assert restored.get_text(' ', strip=True) == block.get_text(' ', strip=True)
                assert [(a.get('href'), a.get('id')) for a in restored.find_all('a')] == [
                    (a.get('href'), a.get('id')) for a in block.find_all('a')]
            reports.append({'variant': variant, 'source_blocks_retained': len(results),
                            'failed_not_successful': translator.stats.failed_chunks, 'bad_cache_writes': 0,
                            'injected_response_batches': len(calls)})
        assert not any(g.called for g in guards)
    assert before == [fingerprint(p) for p in (source, damaged)]
    print(json.dumps({'history_regression': reports, 'real_model_calls': 0, 'originals_unchanged': True}))


if __name__ == '__main__':
    main()
