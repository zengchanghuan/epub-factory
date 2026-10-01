"""R10 opt-in: original historical EPUBs through the independent repair product.

No converter setup is inherited. Two original books have no repairable defect;
they exercise already-paid legacy recovery, not a new charge. The third has a
real old DOCTYPE. The unchanged standalone repair engine supplies a same-source
baseline; independent member-byte assertions limit changes to its narrow scope.
EPUBCheck really runs, but existing source defects are not relabelled as fixed.
"""
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

from test_d37_entitlement_history import BOOKS, sha256
from test_d38_navigation_history import BookSnapshot


class IdentityText:
    def convert(self, value):
        return value


def members(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise AssertionError('Historical ZIP CRC failed')
        return {item.filename: archive.read(item) for item in archive.infolist() if not item.is_dir()}


def expected_repaired_members(source):
    expected = dict(source)
    expected.setdefault('mimetype', b'application/epub+zip')
    for name, raw in source.items():
        if name.endswith(('.xhtml', '.html')):
            text = raw.decode('utf-8', errors='replace')
            changed = re.sub(r'<!DOCTYPE\s+html\s+(?:PUBLIC|SYSTEM)[^>]*>', '<!DOCTYPE html>',
                             text, count=1, flags=re.I | re.S)
        elif name.endswith('.opf'):
            text = raw.decode('utf-8', errors='replace')
            changed = re.sub(r'\s+xmlns=""', '', text)
        else:
            continue
        if changed != text:
            expected[name] = changed.encode('utf-8')
    return expected


def epubcheck(path, jar, report_path):
    process = subprocess.run(['java', '-jar', str(jar), str(path), '--json', str(report_path)],
                             capture_output=True, text=True, timeout=60)
    report = json.loads(report_path.read_text(encoding='utf-8'))
    messages = report['messages']
    checker = report['checker']
    counts = Counter(message['severity'] for message in messages)
    for key, severity in (('nFatal', 'FATAL'), ('nError', 'ERROR'), ('nWarning', 'WARNING')):
        if checker.get(key) != counts[severity]:
            raise AssertionError('EPUBCheck result has inconsistent counters')
    if process.returncode not in (0, 1) or (process.returncode == 0) != (not counts['ERROR'] and not counts['FATAL']):
        raise AssertionError('EPUBCheck process outcome does not match its report')
    errors = Counter()
    for message in messages:
        if message['severity'] in {'ERROR', 'FATAL'}:
            paths = tuple(sorted(location.get('path', '') for location in message.get('locations', [])))
            errors[(message.get('ID', message.get('id', '')), message['severity'], paths)] += 1
    return {'errors': errors, 'counts': {name: counts[name] for name in ('ERROR', 'FATAL', 'WARNING')}}


@unittest.skipUnless(os.environ.get('EPUB_HISTORY_UPLOAD_DIR') and os.environ.get('EPUB_HISTORY_OUTPUT_DIR'),
                     'Explicit historical upload/output directories required; no synthetic replacement.')
class RepairHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uploads = Path(os.environ['EPUB_HISTORY_UPLOAD_DIR']).resolve()
        cls.old_outputs = Path(os.environ['EPUB_HISTORY_OUTPUT_DIR']).resolve()
        cls.assert_originals_unchanged()
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix='epub-r10-history-')))
        cls.stack.enter_context(patch.dict(os.environ, {
            'DATABASE_URL': 'sqlite:///' + str(cls.root / 'bootstrap.db'), 'EPUB_PERSISTENT_STORE': '1',
            'REPAIR_UPLOAD_DIR': str(cls.root / 'bootstrap-repair'), 'REPAIR_CONCURRENCY': '1',
            'REPAIR_PRICE_CNY': '2.99', 'SKIP_PAYMENT_CHECK': '0', 'ALIPAY_APP_ID': '',
            'ALIPAY_DISABLE_PRECREATE': '0',
            'ALIPAY_SELLER_ID': '', 'CELERY_BROKER_URL': '', 'REDIS_URL': '',
            'SENTRY_DSN': '', 'OWNER_PAYMENT_EMAIL_ENABLED': '0', 'NOTIFY_EMAIL_ENABLED': '0',
            'OPENAI_API_KEY': '', 'DEEPSEEK_API_KEY': '', 'DASHSCOPE_API_KEY': '', 'GEMINI_API_KEY': '',
        }))
        cls.stack.enter_context(patch('dotenv.load_dotenv', return_value=False))
        cls.network = [cls.stack.enter_context(patch(target, side_effect=AssertionError('R10 history forbids network')))
                       for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo',
                                      'requests.sessions.Session.request')]
        from app import main
        from app.engine.epub_repairer import diagnose, repair
        from app.engine.compiler import EPUBCHECK_JAR
        cls.main, cls.diagnose = main, staticmethod(diagnose)
        cls.jar = Path(EPUBCHECK_JAR)
        if not cls.jar.is_file():
            raise AssertionError('Actual EPUBCheck JAR is required')
        if hasattr(main.job_store, '_engine'):
            cls.stack.callback(main.job_store._engine.dispose)
        cls.baselines = {}
        for book in BOOKS:
            source = cls.uploads / book['input']
            baseline = cls.root / (book['key'] + '-old-repair.epub')
            original = members(source)
            repair(str(source), str(baseline))
            cls.baselines[book['key']] = {
                'path': baseline, 'original': original, 'expected': expected_repaired_members(original),
                'source_qa': epubcheck(source, cls.jar, cls.root / (book['key'] + '-source.json')),
                'baseline_qa': epubcheck(baseline, cls.jar, cls.root / (book['key'] + '-baseline.json')),
            }
        print('R10 independent repair source/baseline QA: ' + json.dumps({
            key: {'source': row['source_qa']['counts'], 'baseline': row['baseline_qa']['counts']}
            for key, row in cls.baselines.items()}, sort_keys=True))

    @classmethod
    def assert_originals_unchanged(cls):
        for book in BOOKS:
            for kind, directory in (('input', cls.uploads), ('output', cls.old_outputs)):
                if sha256(directory / book[kind]) != book[kind + '_sha256']:
                    raise AssertionError('Pinned original or historical artifact changed: ' + book['key'] + ':' + kind)

    @classmethod
    def tearDownClass(cls):
        cls.assert_originals_unchanged()
        for guard in cls.network:
            guard.assert_not_called()

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.domain.repair_executor import RepairExecutor
        self.case = ExitStack()
        self.addCleanup(self.case.close)
        self.case_root = Path(self.case.enter_context(tempfile.TemporaryDirectory(prefix='case-', dir=self.root)))
        self.executor = RepairExecutor(concurrency=1)
        self.case.enter_context(patch.object(self.main, '_REPAIR_UPLOAD_DIR', self.case_root))
        self.case.enter_context(patch.object(self.main, '_repair_executor', self.executor))
        self.email = self.case.enter_context(patch.object(self.main, '_queue_repair_completion_email'))
        self.gateway = self.case.enter_context(patch('app.infra.alipay.create_alipay_precreate', return_value='alipay://offline'))
        self.query = self.case.enter_context(patch('app.infra.alipay.query_verified_trade', side_effect=AssertionError('No historical payment query')))
        app = FastAPI()
        for suffix, handler, verb in (
            ('/diagnose', self.main.repair_diagnose, 'POST'), ('/{job_id}/pay', self.main.repair_pay, 'POST'),
            ('/{job_id}/status', self.main.repair_status, 'GET'), ('/{job_id}/recover', self.main.repair_recover_payment, 'POST'),
            ('/{job_id}/download', self.main.repair_download, 'GET'),
        ):
            app.add_api_route('/api/v2/repair' + suffix, handler, methods=[verb])
        self.client = self.case.enter_context(TestClient(app))
        # Drain accepted work before undoing its repository/email patches, even
        # when a failed assertion interrupts a test while a thread is active.
        self.case.callback(self.executor.shutdown)

    def tearDown(self):
        self.assert_originals_unchanged()
        self.query.assert_not_called()

    def legacy(self, book, *, repaired=False):
        job_id = uuid.uuid4().hex
        directory = self.case_root / job_id
        directory.mkdir()
        source = directory / book['input']
        shutil.copyfile(self.uploads / book['input'], source)
        metadata = {'status': 'paid', 'filename': source.name, 'expected_amount': '5.99',
                    'out_trade_no': 'repair_' + job_id}
        if repaired:
            filename = source.stem + '_fixed.epub'
            shutil.copyfile(self.baselines[book['key']]['path'], directory / filename)
            metadata.update(status='repaired', download_filename=filename)
        # Genuine old on-disk schema, without owner/attempt/artifact_file fields.
        (directory / 'order.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        return job_id, source

    def wait_repaired(self, job_id):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = self.main._repair_job_get(job_id)
            if job['status'] != 'paid':
                self.assertEqual(job['status'], 'repaired', job.get('error'))
                return job
            time.sleep(.01)
        self.fail('Historical independent repair did not complete')

    def assert_whitelisted_result(self, book, artifact):
        row = self.baselines[book['key']]
        self.assertEqual(members(artifact), row['expected'])
        with zipfile.ZipFile(artifact) as archive:
            self.assertEqual(archive.namelist()[0], 'mimetype')
            self.assertEqual(archive.getinfo('mimetype').compress_type, zipfile.ZIP_STORED)
        before = BookSnapshot(self.uploads / book['input'], IdentityText())
        after = BookSnapshot(artifact, IdentityText())
        self.assertEqual(after.images, before.images)
        self.assertEqual(set(after.docs), set(before.docs))
        for name in before.docs:
            self.assertEqual(after.docs[name]['text'], before.docs[name]['text'])
            self.assertEqual(after.docs[name]['ids'], before.docs[name]['ids'])
        self.assertEqual(after.toc, before.toc)
        self.assertEqual(after.navigation, before.navigation)
        report = epubcheck(artifact, self.jar, self.case_root / (book['key'] + '-final.json'))
        self.assertFalse(report['errors'] - row['baseline_qa']['errors'], 'New repair execution added EPUBCheck errors')
        self.assertEqual(report['counts'], row['baseline_qa']['counts'])
        return report

    def test_same_source_old_repair_baseline_only_changes_whitelisted_format(self):
        expected_counts = {'double-helix': 0, 'die-with-zero': 1, 'responsibility-and-judgement': 0}
        for book in BOOKS:
            with self.subTest(book=book['key']):
                row = self.baselines[book['key']]
                self.assertEqual(self.diagnose(str(self.uploads / book['input'])).fixable_count, expected_counts[book['key']])
                self.assertEqual(members(row['path']), row['expected'])
                changed = {name for name in row['original'] if row['original'][name] != row['expected'][name]}
                self.assertEqual(changed, {'item/navigation-documents.xhtml'} if book['key'] == 'die-with-zero' else set())
                self.assertFalse(row['baseline_qa']['errors'] - row['source_qa']['errors'], 'Old repair algorithm introduced new source errors')

    def test_three_original_legacy_paid_orders_repair_once_refresh_and_download(self):
        from app.engine.epub_repairer import repair
        reports = {}
        with patch('app.engine.epub_repairer.repair', wraps=repair) as engine:
            for book in BOOKS:
                with self.subTest(book=book['key']):
                    job_id, source = self.legacy(book)
                    self.assertEqual(self.client.get(f'/api/v2/repair/{job_id}/download').status_code, 402)
                    self.assertTrue(self.main._ensure_repair_running(job_id))
                    job = self.wait_repaired(job_id)
                    self.assertEqual(job['execution_attempts'], 1)
                    self.assertEqual(job['expected_amount'], '5.99')
                    artifact = self.case_root / job_id / job['artifact_file']
                    reports[book['key']] = self.assert_whitelisted_result(book, artifact)['counts']
                    digest = sha256(artifact)
                    self.assertEqual(job['artifact_sha256'], digest)
                    self.assertEqual(sha256(source), book['input_sha256'])
                    for _ in range(2):
                        self.assertFalse(self.main._ensure_repair_running(job_id))
                        status = self.client.get(f'/api/v2/repair/{job_id}/status')
                        self.assertEqual(status.json()['status'], 'repaired')
                        self.assertEqual(status.json()['price_cny'], '5.99')
                        downloaded = self.client.get(f'/api/v2/repair/{job_id}/download')
                        self.assertEqual(downloaded.status_code, 200)
                        self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), digest)
                    self.assertFalse(list((self.case_root / job_id).glob('*.pending.epub')))
            self.assertEqual(engine.call_count, 3)
        self.gateway.assert_not_called()
        self.assertEqual(self.email.call_count, 3)
        print('R10 real repair delivery QA (residual original errors retained): ' + json.dumps(reports, sort_keys=True))

    def test_zero_problem_originals_cannot_start_new_paid_checkout(self):
        for book in BOOKS:
            with self.subTest(book=book['key']):
                self.gateway.reset_mock()
                with (self.uploads / book['input']).open('rb') as source:
                    uploaded = self.client.post('/api/v2/repair/diagnose', files={
                        'file': (book['input'], source, 'application/epub+zip')})
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                info = uploaded.json()
                fixable = book['key'] == 'die-with-zero'
                self.assertEqual(info['can_pay'], fixable)
                response = self.client.post(f"/api/v2/repair/{info['job_id']}/pay")
                self.assertEqual(response.status_code, 200 if fixable else 409, response.text)
                if fixable:
                    self.gateway.assert_called_once()
                    self.assertEqual(response.json()['price_cny'], '2.99')
                else:
                    self.gateway.assert_not_called()

    def test_legacy_repaired_artifacts_remain_byte_identical_without_execution(self):
        with patch('app.engine.epub_repairer.repair', side_effect=AssertionError('Delivered legacy repair must not rerun')):
            for book in BOOKS:
                with self.subTest(book=book['key']):
                    job_id, source = self.legacy(book, repaired=True)
                    metadata = self.case_root / job_id / 'order.json'
                    before = metadata.read_bytes()
                    self.assertFalse(self.main._ensure_repair_running(job_id))
                    status = self.client.get(f'/api/v2/repair/{job_id}/status').json()
                    self.assertEqual(status['status'], 'repaired')
                    self.assertEqual(status['price_cny'], '5.99')
                    downloaded = self.client.get(f'/api/v2/repair/{job_id}/download')
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), sha256(self.baselines[book['key']]['path']))
                    self.assertEqual(metadata.read_bytes(), before)
                    self.assertEqual(sha256(source), book['input_sha256'])
        self.email.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
