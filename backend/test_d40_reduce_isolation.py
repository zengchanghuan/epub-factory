"""R2 offline full-resource/attempt isolation and rescue-regression gates."""
import json
import os
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ebooklib import epub

from app.domain import book_reduce_service as reducer
from app.domain.manifest_service import _unique_chapter_ids, build_manifest


class ReduceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scope = patch.object(reducer, '_REDUCE_WORK_DIR', self.root / 'reduce')
        scope.start(); self.addCleanup(scope.stop)
        env = patch.dict(os.environ, {'EPUB_FAILED_CHUNK_DIR': str(self.root / 'failed'),
                                     'EPUB_TRANSLATION_CHECKPOINT_DB': str(self.root / 'checkpoints.db')})
        env.start(); self.addCleanup(env.stop)
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('No network in R2 tests'))
        network.start(); self.addCleanup(network.stop)

    def put(self, path, content, attempt='attempt-1', job='job-1'):
        return reducer.set_chapter_output(job, path, content, attempt_id=attempt)

    def get(self, path, attempt='attempt-1', job='job-1'):
        return reducer.get_chapter_output(job, path, attempt_id=attempt)

    def test_same_basename_in_different_directories_does_not_collide(self):
        left = self.put('part1/chapter.xhtml', b'Alpha')
        right = self.put('part2/chapter.xhtml', b'Beta')
        self.assertNotEqual(left, right)
        self.assertEqual(self.get('part1/chapter.xhtml'), b'Alpha')
        self.assertEqual(self.get('part2/chapter.xhtml'), b'Beta')

    def test_literal_uri_characters_case_and_unicode_forms_are_distinct(self):
        paths = ['Text/a%20b.xhtml', 'Text/a b.xhtml', 'a%2Fb.xhtml', 'a/b.xhtml',
                 'Text/A.xhtml', 'Text/a.xhtml', 'Text/é.xhtml', 'Text/e\u0301.xhtml',
                 'Text/a#b.xhtml', 'Text/a?b.xhtml']
        locations = []
        for number, path in enumerate(paths):
            locations.append(self.put(path, str(number).encode()))
        self.assertEqual(len(locations), len(set(locations)))
        for number, path in enumerate(paths):
            self.assertEqual(self.get(path), str(number).encode())

    def test_invalid_paths_and_scope_components_fail_before_any_write(self):
        for path in ['', '.', '..', '../chapter.xhtml', '/chapter.xhtml', 'a/../b.xhtml',
                     'a/./b.xhtml', 'a//b.xhtml', 'a/', 'a\\b.xhtml', 'C:/a.xhtml', 'a\x00b.xhtml', 'a\nb.xhtml']:
            with self.subTest(path=repr(path)), self.assertRaises(ValueError):
                self.put(path, b'body')
        for value in ['', '.', '..', '../job', '/tmp/job', 'a/b', 'a\\b', 'a%2Fb', 'a\n', 'a' * 129]:
            for field in ['job', 'attempt']:
                with self.subTest(value=value, field=field), self.assertRaises(ValueError):
                    self.put('a.xhtml', b'body', **{field: value})
        self.assertFalse((self.root / 'reduce').exists())

    def test_attempt_getter_is_frozen_and_old_late_write_cannot_pollute_new_attempt(self):
        attempt = 'attempt-1'
        old = reducer.make_get_chapter_content('job-1', attempt_id=attempt)
        attempt = 'attempt-2'
        new = reducer.make_get_chapter_content('job-1', attempt_id=attempt)
        self.put('a.xhtml', b'new', attempt)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(self.put, 'a.xhtml', b'late-old', 'attempt-1').result()
        self.assertEqual(old('a.xhtml'), b'late-old')
        self.assertEqual(new('a.xhtml'), b'new')
        self.assertIsNone(self.get('a.xhtml', job='other-job'))

    def test_scope_identity_case_differences_do_not_alias_on_mac_filesystems(self):
        scopes = [('Job-A', 'Attempt-A'), ('job-a', 'Attempt-A'), ('Job-A', 'attempt-a')]
        locations = []
        for number, (job, attempt) in enumerate(scopes):
            locations.append(self.put('a.xhtml', str(number).encode(), attempt, job))
        self.assertEqual(len({str(path).lower() for path in locations}), len(scopes))
        for number, (job, attempt) in enumerate(scopes):
            self.assertEqual(self.get('a.xhtml', attempt, job), str(number).encode())

    def test_chapter_task_captures_attempt_before_a_late_result(self):
        from types import SimpleNamespace
        from app.tasks import translate as task_module
        from app.domain import chapter_translation_service as chapter_module
        from app.cancellation import JobCancelled
        from app.models import JobStatus
        job = SimpleNamespace(status=JobStatus.running, translation_stats={'attempt_id': 'attempt-1'})
        def late_result(_job_id, _chapter_id):
            job.translation_stats = {'attempt_id': 'attempt-2'}
            return SimpleNamespace(job_id='job-1', chapter_id='chapter', file_path='chapter.xhtml',
                                   reduced_html=b'<p>late</p>', chapter_kind='body', skipped=False,
                                   error=None, chunks=[])
        with patch.object(chapter_module.job_store, 'get', return_value=job), \
             patch.object(chapter_module.job_store, 'get_execution', return_value={
                 'state': 'running', 'owner': 'owner-1', 'attempt_id': 'attempt-1'}), \
             patch.object(task_module, 'translate_chapter', side_effect=late_result) as translate:
            # R8 requires an explicitly captured owner and rejects the result
            # after retry, not merely a different-attempt disk destination.
            with self.assertRaises(JobCancelled):
                task_module.translate_chapter_task.run('job-1', 'chapter', 'attempt-1', 'owner-1')
            self.assertIsNone(self.get('chapter.xhtml', 'attempt-1'))
            self.assertIsNone(reducer.get_chapter_output('job-1', 'chapter.xhtml',
                              attempt_id='attempt-1', execution_owner='owner-1'))
            self.assertIsNone(self.get('chapter.xhtml', 'attempt-2'))
            translate.reset_mock()
            with self.assertRaises(JobCancelled):
                task_module.translate_chapter_task.run('job-1', 'chapter', 'attempt-1', 'owner-1')
            translate.assert_not_called()

    def test_legacy_basename_files_are_never_implicitly_read_or_overwritten(self):
        old = self.root / 'reduce' / 'job-1' / 'reduced' / 'chapter.xhtml'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'unsafe legacy')
        self.assertIsNone(self.get('one/chapter.xhtml'))
        self.assertIsNone(reducer.get_chapter_output('job-1', 'one/chapter.xhtml'))
        reducer.set_chapter_output('job-1', 'one/chapter.xhtml', b'new legacy API')
        self.assertEqual(reducer.make_get_chapter_content('job-1')('one/chapter.xhtml'), b'new legacy API')
        self.assertIsNone(self.get('one/chapter.xhtml'))
        self.assertEqual(old.read_bytes(), b'unsafe legacy')

    def test_concurrent_writes_and_reads_only_observe_whole_verified_artifacts(self):
        payloads = [b'A' * 131072, b'B' * 131072]
        self.put('a.xhtml', payloads[0])
        start = threading.Barrier(3)
        def writer(payload):
            start.wait()
            for _ in range(12):
                self.put('a.xhtml', payload)
        def reader():
            start.wait()
            for _ in range(60):
                self.assertIn(self.get('a.xhtml'), payloads)
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(writer, payload) for payload in payloads] + [executor.submit(reader)]
            for future in futures:
                future.result()
        self.assertEqual(list((self.root / 'reduce').rglob('.tmp-*')), [])

    def test_failed_atomic_replace_preserves_previous_output_and_cleans_temporary(self):
        self.put('a.xhtml', b'previous')
        with patch.object(reducer.os, 'replace', side_effect=OSError('interrupted replace')):
            with self.assertRaises(OSError):
                self.put('a.xhtml', b'next')
        self.assertEqual(self.get('a.xhtml'), b'previous')
        self.assertEqual(list((self.root / 'reduce').rglob('.tmp-*')), [])

    def test_symlink_attempt_directory_and_leaf_cannot_escape_scope(self):
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = outside / 'sentinel'
        sentinel.write_bytes(b'keep')
        path = self.put('a.xhtml', b'old')
        path.unlink(); path.symlink_to(sentinel)
        with self.assertRaises((OSError, ValueError)):
            self.get('a.xhtml')
        with self.assertRaises((OSError, ValueError)):
            self.put('a.xhtml', b'bad')
        path.unlink()
        directory = path.parent
        directory.rmdir(); directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            self.put('a.xhtml', b'bad')
        with self.assertRaises((OSError, ValueError)):
            self.get('a.xhtml')
        self.assertEqual(sentinel.read_bytes(), b'keep')
        self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_scope_root_symlink_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'reduce').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.put('a.xhtml', b'bad')
        self.assertEqual(list(outside.iterdir()), [])

    def test_identity_checksum_corruption_and_empty_payload_fail_closed(self):
        path = self.put('a.xhtml', b'original')
        original = path.read_bytes()
        for field, wrong in [('file_path', 'b.xhtml'), ('attempt_id', 'attempt-2'),
                             ('job_id', 'other'), ('sha256', '0' * 64), ('content', ''), ('schema', 1)]:
            with self.subTest(field=field):
                payload = json.loads(original); payload[field] = wrong
                path.write_text(json.dumps(payload), encoding='utf-8')
                with self.assertRaises(ValueError): self.get('a.xhtml')
        for content in [b'', b' \n\t']:
            with self.assertRaises(ValueError): self.put('b.xhtml', content)
        getter = reducer.make_get_chapter_content('job-1', attempt_id='new', required_files=['a.xhtml'])
        with self.assertRaises(FileNotFoundError): getter('a.xhtml')
        self.assertIsNone(getter('untranslated-nav.xhtml'))

    def test_manifest_ids_are_unique_deterministic_bounded_and_preserve_unique_old_ids(self):
        paths = ['part1/chapter.xhtml', 'part2/chapter.xhtml', 'a-b.xhtml', 'a b.xhtml', 'unique.xhtml']
        ids, ambiguous = _unique_chapter_ids(paths)
        self.assertEqual(ids['unique.xhtml'], 'unique')
        self.assertEqual(set(ambiguous), {'chapter', 'a_b'})
        self.assertEqual(len(set(ids.values())), len(paths))
        self.assertTrue(all(len(value) <= 64 for value in ids.values()))
        self.assertEqual(_unique_chapter_ids(list(reversed(paths)))[0], ids)

    def test_generated_id_cannot_collide_with_an_original_unique_stem(self):
        paths = ['one/chapter.xhtml', 'two/chapter.xhtml']
        generated = _unique_chapter_ids(paths)[0][paths[0]]
        occupied = generated + '.xhtml'
        ids, _ = _unique_chapter_ids([*paths, occupied])
        self.assertEqual(ids[occupied], generated)
        self.assertEqual(len(set(ids.values())), 3)
        with self.assertRaises(ValueError): _unique_chapter_ids([paths[0], paths[0]])

    def make_book(self, paths_and_text):
        book = epub.EpubBook(); book.set_identifier('r2-offline'); book.set_title('R2'); book.set_language('en')
        for path, text in paths_and_text:
            chapter = epub.EpubHtml(title='Chapter', file_name=path)
            chapter.content = f'<html><body><p>{text}</p></body></html>'
            book.add_item(chapter); book.spine.append(chapter)
            book.toc += (epub.Link(path, 'Chapter', chapter.id),)
        book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
        source = self.root / 'source.epub'; epub.write_epub(str(source), book)
        return source

    def test_real_manifest_rejects_old_ambiguous_and_renamed_long_strategy_ids(self):
        from app.domain.fast_translation_runner import _validate_manifest_identity
        long_stem = 'z' * 90
        source = self.make_book([('one/chapter.xhtml', 'Alpha'), ('two/chapter.xhtml', 'Beta'),
                                 (long_stem + '.xhtml', 'Long')])
        manifest = build_manifest(str(source), 'job-1')
        ids = [chapter['chapter_id'] for chapter in manifest['chapters']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(len(value) <= 64 for value in ids))
        self.assertIn(long_stem, manifest['renamed_legacy_chapter_ids'])
        for old in ['chapter', long_stem]:
            with self.assertRaisesRegex(ValueError, '重新确认'):
                _validate_manifest_identity(manifest, {old: 'academic'})
        _validate_manifest_identity(manifest, {ids[0]: 'academic'})
        manifest['chapters'][1]['chapter_id'] = ids[0]
        with self.assertRaises(ValueError): _validate_manifest_identity(manifest)

if __name__ == '__main__': unittest.main()
