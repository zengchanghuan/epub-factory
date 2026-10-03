"""Opt-in opaque-byte regression: three real EPUBs and the selected PDF.

Order metadata is explicitly synthetic; no live orders, payments or files are
changed. This verifies inventory classification and preservation, not conversion.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/audit-artifact-retention.py'
spec = importlib.util.spec_from_file_location('retention_history_subject', SCRIPT)
subject = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = subject
spec.loader.exec_module(subject)
PDF_SHA = 'af21894c7542c1a7e3c286a762cc15c1d1aeaa81fb9ca8e028d85ff5d22b5023'
PDF_EPUB_SHA = '3ee8767ea0659d9c9f588e0b22390044008406445b59056ec73f0271b62b8b60'
MANUAL_SHA = '874487e8cc4bd7d0acdfc7521073d8f78a4398a1e4bc5faa293a76480933a73b'
ARGS = None


def fingerprint(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    after = path.stat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
    if identity(before) != identity(after):
        raise AssertionError('Real asset changed during fingerprint')
    return identity(after), digest.hexdigest()


class RetentionHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if ARGS is None:
            raise unittest.SkipTest('explicit real assets not supplied; historical gate not executed')
        manifest = json.loads(ARGS.assets_manifest.read_text())
        assert manifest['status'] == 'passed' and manifest['books']['ok']
        selected = manifest['books']['files']
        assert len(selected) == 9
        assert {row['book'] for row in selected} == {
            'double-helix', 'die-with-zero', 'responsibility-and-judgement'}
        cls.originals = {}
        for row in selected:
            path = Path(row['path'])
            actual = fingerprint(path)
            assert actual[1] == row['sha256'] == row['expected_sha256']
            cls.originals[path] = actual
        for path, expected in ((ARGS.pdf, PDF_SHA), (ARGS.pdf_epub, PDF_EPUB_SHA),
                               (ARGS.manual_epub, MANUAL_SHA)):
            actual = fingerprint(path)
            assert actual[1] == expected
            cls.originals[path] = actual
        cls.addClassCleanup(cls.assert_originals_unchanged)
        temporary = tempfile.TemporaryDirectory(prefix='retention-real-assets-')
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name).resolve()
        cls.roots = {name: cls.root / name for name in ('uploads', 'outputs', 'repair', 'reduce_work')}
        for root in cls.roots.values():
            root.mkdir()
        cls.database = cls.root / 'explicit-offline-backup.sqlite'
        cls.book_paths, cls.copies = {}, {}
        for row in selected:
            label = 'uploads' if row['role'] == 'input' else 'outputs'
            name = row['book'] + '-' + row['role'] + '.epub'
            destination = cls.roots[label] / name
            shutil.copyfile(row['path'], destination)
            cls.book_paths[(row['book'], row['role'])] = destination
            cls.copies[destination] = fingerprint(destination)
        pdf_source = cls.roots['uploads'] / 'chinese-source.pdf'
        shutil.copyfile(ARGS.pdf, pdf_source)
        cls.copies[pdf_source] = fingerprint(pdf_source)
        cls.aid = 'd' * 32
        prepared = cls.roots['outputs'] / ('.pdf-prepared-' + cls.aid)
        prepared.mkdir()
        shutil.copyfile(ARGS.pdf_epub, prepared / 'book.epub')
        cls.copies[prepared / 'book.epub'] = fingerprint(prepared / 'book.epub')
        repair = cls.roots['repair'] / ('e' * 32)
        repair.mkdir()
        for name, original in (('source.epub', cls.book_paths[('double-helix', 'input')]),
                               ('artifact.epub', cls.book_paths[('double-helix', 'output')])):
            shutil.copyfile(original, repair / name)
            cls.copies[repair / name] = fingerprint(repair / name)
        (repair / 'order.json').write_text(json.dumps({'status': 'repaired', 'filename': 'source.epub',
            'artifact_file': 'artifact.epub', 'access_token': 'PRIVATE-RETENTION-TOKEN',
            'qr_code': 'https://invalid.example/PRIVATE-QR'}))
        # Deliberately independent schema projection, not imported from the
        # subject's TABLES or tests. All tables exist in the actual JobStore.
        with sqlite3.connect(cls.database) as db:
            db.executescript('''
                CREATE TABLE epub_jobs(id TEXT PRIMARY KEY,input_path TEXT,output_path TEXT,status TEXT,
                    batch_id TEXT,translation_stats_json TEXT,payment_entitlement_json TEXT,payment_resolution_json TEXT);
                CREATE TABLE job_dispatch_outbox(job_id TEXT,attempt_id TEXT,status TEXT);
                CREATE TABLE job_executions(job_id TEXT,attempt_id TEXT,owner TEXT,state TEXT);
                CREATE TABLE admin_order_reviews(order_no TEXT,state TEXT);
                CREATE TABLE admin_order_review_events(order_no TEXT,result_json TEXT);
                CREATE TABLE order_funnel_events(order_no TEXT,event TEXT);
            ''')
            for index, book in enumerate(sorted({row['book'] for row in selected})):
                job = str(index + 1) * 12
                db.execute('INSERT INTO epub_jobs VALUES(?,?,?,?,?,?,?,?)', (job,
                    str(cls.book_paths[(book, 'input')]), str(cls.book_paths[(book, 'output')]),
                    'failed' if index == 1 else 'success', None,
                    json.dumps({'attempt_id': 'PRIVATE-ATTEMPT', 'private': 'PRIVATE-RETENTION-TOKEN'}),
                    '{}', json.dumps({'state': 'paid', 'source': 'synthetic-test-only'})))
                if index == 1:
                    db.execute('INSERT INTO admin_order_reviews VALUES(?,?)', (job, 'open'))
            db.execute('INSERT INTO epub_jobs VALUES(?,?,?,?,?,?,?,?)', ('4' * 12, str(pdf_source), None,
                'awaiting_confirm', None, json.dumps({'attempt_id': 'pdf-private-attempt', 'pdf_conversion': {
                    'schema_version': 1, 'product': 'pdf_text_conversion', 'phase': 'prepared',
                    'artifact_id': cls.aid, 'plan_id': 'a' * 64}}), '{}', '{}'))
            db.commit()
        cls.db_before = fingerprint(cls.database)
        cls.report = subject.audit(cls.database, **cls.roots)
        cls.files = {(row['root'], row['relative_path']): row for row in cls.report['files']}

    @classmethod
    def assert_originals_unchanged(cls):
        for path, expected in cls.originals.items():
            if fingerprint(path) != expected:
                raise AssertionError('An original historical asset changed')

    def test_all_three_real_epub_originals_and_outputs_are_protected(self):
        for (book, role), path in self.book_paths.items():
            label = 'uploads' if role == 'input' else 'outputs'
            row = self.files[(label, path.name)]
            if role != 'baseline':
                self.assertEqual(row['classification'], 'protected_reference')
                self.assertTrue(row['owners'])
            else:
                self.assertEqual(row['classification'], 'unknown_protected')
            self.assertEqual(row['sha256'], self.copies[path][1])

    def test_real_pdf_source_and_prepared_epub_remain_protected(self):
        source = self.files[('uploads', 'chinese-source.pdf')]
        artifact = self.files[('outputs', '.pdf-prepared-' + self.aid + '/book.epub')]
        self.assertEqual(source['classification'], 'protected_reference')
        self.assertEqual(artifact['classification'], 'protected_reference')
        self.assertEqual(source['sha256'], PDF_SHA)
        self.assertEqual(artifact['sha256'], PDF_EPUB_SHA)
        self.assertIn('pdf_prepared_artifact', artifact['reasons'])

    def test_actual_epub_bytes_in_repair_order_are_preserved(self):
        for name in ('source.epub', 'artifact.epub'):
            row = self.files[('repair', 'e' * 32 + '/' + name)]
            self.assertEqual(row['classification'], 'protected_reference')
            self.assertIn('repair_paid_record_retained', row['reasons'])
        self.assertFalse(self.report['deletion_authorized'])
        self.assertTrue(self.report['consistent_scan'])
        self.assertFalse(self.report['missing_references'])

    def test_cli_reads_real_assets_without_leaking_private_metadata_or_creating_files(self):
        before = {str(p.relative_to(self.root)): fingerprint(p) for p in self.root.rglob('*') if p.is_file()}
        command = [sys.executable, '-I', '-B', str(SCRIPT), '--database', str(self.database)]
        for name, path in self.roots.items():
            command += ['--' + name.replace('_', '-'), str(path)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(self.root)}, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertFalse(reply['deletion_authorized'])
        for secret in ('PRIVATE-RETENTION-TOKEN', 'PRIVATE-QR', 'PRIVATE-ATTEMPT', str(self.root)):
            self.assertNotIn(secret, result.stdout + result.stderr)
        after = {str(p.relative_to(self.root)): fingerprint(p) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_final_originals_copies_database_and_manual_delivery_are_unchanged(self):
        self.assert_originals_unchanged()
        self.assertEqual(fingerprint(self.database), self.db_before)
        for path, expected in self.copies.items():
            self.assertEqual(fingerprint(path), expected)


def main():
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for flag in ('assets-manifest', 'pdf', 'pdf-epub', 'manual-epub'):
        parser.add_argument('--' + flag, required=True, type=Path)
    ARGS = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestLoader().loadTestsFromTestCase(RetentionHistoryTests))
    passed = result.wasSuccessful() and result.testsRun == 5 and not result.skipped
    if passed:
        print(json.dumps({'passed': True, 'tests_run': 5, 'skipped': 0, 'original_assets': 12,
            'copies': len(RetentionHistoryTests.copies), 'read_only': True, 'deletion_authorized': False,
            'real_file_bytes_preserved': True, 'order_metadata': 'isolated synthetic mappings, not live orders'}))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
