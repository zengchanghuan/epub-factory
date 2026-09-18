"""Offline reducer parity and bounded DOM traversal, without model requests.

Run with --benchmark for an optional non-asserted timing comparison against
the previous per-locator sibling scans. Timing is not a portable CI threshold.
"""
import argparse
import json
import time
import unittest
from statistics import median
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

from app.domain import chapter_reduce_service as reducer


def chunk(locator, translation, sequence=1):
    return SimpleNamespace(locator=locator, translated_html=translation,
                           sequence=sequence, chunk_id=f'chunk-{sequence}')


class LegacyLocatorLookup:
    """Use the original sibling-scanning resolver with the same write path."""
    def __init__(self, soup):
        self.soup = soup

    def get(self, locator):
        return reducer.get_node_by_locator(self.soup, locator)


def sibling_fixture(size):
    source = ('<html><body>' + ''.join(
        f'<p id="p{i}">Repeated source paragraph.</p>' for i in range(size)
    ) + '</body></html>').encode()
    chunks = [chunk(f'/html/body/p[{i + 1}]', f'译文{i}。', i) for i in range(size)]
    return source, chunks


class ReducerIndexTests(unittest.TestCase):
    def test_index_matches_legacy_locator_semantics_by_identity(self):
        soup = BeautifulSoup('<html><body><p>Same</p><div><p>Nested</p></div>'
                             '<p>Same</p></body></html>', 'html.parser')
        index = reducer._index_nodes_by_locator(soup)
        locators = [
            '/html/body/p', '/html[1]/body[1]/p[2]',
            '/HTML/BODY/P[02]', '/html//body/div/p',
            '/html/body/ div [1]/p', '/html/body/p[0]',
            '/html/body/p[-1]', '/html/body/p[3]',
            '/body/p', '/html/body/p[bad]', '', '/', ' ',
        ]
        for locator in locators:
            with self.subTest(locator=locator):
                self.assertIs(index.get(reducer._canonical_locator(locator)),
                              reducer.get_node_by_locator(soup, locator))
        self.assertIsNot(index['/html[1]/body[1]/p[1]'], index['/html[1]/body[1]/p[2]'])

    def test_output_matches_scanning_lookup_for_mixed_markup_and_modes(self):
        source = ('<html><head><title>Fixture</title></head><body id="body">'
                  '<h2 id="heading">Heading</h2><p id="first">Same <a id="anchor" href="#heading">note</a></p>'
                  '<div><p>Same</p><p>Same</p></div><p id="last">Last</p></body></html>').encode()
        chunks = [
            chunk('/html/body/p[2]', '<p>结尾</p>', 5),
            chunk('/html/body/h2', '<h2>标题</h2>', 1),
            chunk('/html/body/div/p[2]', '第二重复段', 4),
            chunk('/html/body/p', '<p>正文 <a id="anchor" href="#heading">注释</a></p>', 2),
            chunk('/html/body/div/p', '第一重复段', 3),
        ]
        for bilingual in (False, True):
            with self.subTest(bilingual=bilingual):
                actual = reducer.apply_chunk_results(source, chunks, bilingual)
                with patch.object(reducer, '_index_nodes_by_locator', side_effect=LegacyLocatorLookup):
                    reference = reducer.apply_chunk_results(source, chunks, bilingual)
                self.assertEqual(actual, reference)
                parsed = BeautifulSoup(actual, 'html.parser')
                self.assertEqual(len(parsed.find_all(id='anchor')), 1)
                self.assertEqual(parsed.body['id'], 'body')
                self.assertEqual(parsed.find(id='anchor')['href'], '#heading')
                paragraphs = parsed.div.find_all('p', recursive=False)
                if bilingual:
                    self.assertEqual(paragraphs[0].select_one('.epub-original').get_text(), 'Same')
                    self.assertEqual(paragraphs[0].select_one('.epub-translated').get_text(), '第一重复段')
                    self.assertEqual(paragraphs[1].select_one('.epub-translated').get_text(), '第二重复段')
                else:
                    self.assertEqual([p.get_text() for p in paragraphs], ['第一重复段', '第二重复段'])

    def test_invalid_locators_do_not_change_neighbouring_paragraphs(self):
        source = b'<html><body><p>Keep first</p><p>Keep second</p></body></html>'
        chunks = [chunk('/html/body/p[0]', 'wrong zero'),
                  chunk('/html/body/p[3]', 'wrong range'),
                  chunk('/html/body/p[-1]', 'wrong negative'),
                  chunk('/html/body/missing', 'wrong tag'),
                  chunk('', 'wrong empty')]
        parsed = BeautifulSoup(reducer.apply_chunk_results(source, chunks, False), 'html.parser')
        self.assertEqual([p.get_text() for p in parsed.find_all('p')], ['Keep first', 'Keep second'])

    def test_original_node_bindings_are_not_retargeted_into_new_translation(self):
        # Normal extraction returns leaf blocks. Even malformed overlapping
        # results must not make a later locator overwrite newly inserted text.
        source = b'<html><body><div><p>Original</p></div><p>Sibling</p></body></html>'
        chunks = [chunk('/html/body/div', '<div><p>新内容</p></div>', 1),
                  chunk('/html/body/div/p', '不能覆盖新内容', 2),
                  chunk('/html/body/p', '相邻段译文', 3)]
        parsed = BeautifulSoup(reducer.apply_chunk_results(source, chunks, False), 'html.parser')
        self.assertEqual(parsed.div.p.get_text(), '新内容')
        self.assertEqual(parsed.body.find('p', recursive=False).get_text(), '相邻段译文')

    def test_large_chapter_builds_one_index_without_per_chunk_sibling_scans(self):
        source, chunks = sibling_fixture(2000)
        with patch.object(reducer, '_index_nodes_by_locator', wraps=reducer._index_nodes_by_locator) as index, \
             patch.object(reducer, 'get_node_by_locator', side_effect=AssertionError('per-chunk lookup')), \
             patch.object(reducer, '_get_direct_children', side_effect=AssertionError('sibling scan')):
            output = reducer.apply_chunk_results(source, chunks, False)
        self.assertEqual(index.call_count, 1)
        paragraphs = BeautifulSoup(output, 'html.parser').find_all('p')
        self.assertEqual(len(paragraphs), 2000)
        self.assertTrue(all(p['id'] == f'p{i}' and p.get_text() == f'译文{i}。'
                            for i, p in enumerate(paragraphs)))

    def test_fragment_without_html_wrapper_and_duplicate_chunks(self):
        source = b'<p>First</p><p>Second</p>'
        chunks = [chunk('/p[2]', '第二段', 3), chunk('/p', '旧译文', 1), chunk('/p', '最终译文', 2)]
        output = reducer.apply_chunk_results(source, chunks, False)
        self.assertEqual([p.get_text() for p in BeautifulSoup(output, 'html.parser').find_all('p')],
                         ['最终译文', '第二段'])


def benchmark():
    """Compare identical reducer work with indexed versus legacy lookup."""
    for size in (1000, 3000, 6000):
        source, chunks = sibling_fixture(size)
        runs = {'indexed': [], 'legacy_scans': []}
        output = {}
        for _ in range(3):
            for mode in runs:
                factory = LegacyLocatorLookup if mode == 'legacy_scans' else reducer._index_nodes_by_locator
                with patch.object(reducer, '_index_nodes_by_locator', side_effect=factory):
                    started = time.perf_counter()
                    output[mode] = reducer.apply_chunk_results(source, chunks, False)
                    runs[mode].append(time.perf_counter() - started)
        assert output['indexed'] == output['legacy_scans']
        print(json.dumps({'paragraphs': size, 'output_identical': True,
                          **{f'{mode}_median_seconds': round(median(values), 6)
                             for mode, values in runs.items()}}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--benchmark', action='store_true')
    args, remaining = parser.parse_known_args()
    if args.benchmark:
        benchmark()
    else:
        unittest.main(argv=[__file__, *remaining], verbosity=2)
