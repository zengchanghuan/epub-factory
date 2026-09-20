"""Offline release checks: existing books remain discoverable and downloadable."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values

_runtime = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + _runtime.name + '/jobs.db'
os.environ['REPAIR_UPLOAD_DIR'] = _runtime.name + '/repair-jobs'
os.environ['OPENAI_API_KEY'] = 'offline-test-only'
os.environ['ALIPAY_APP_ID'] = ''
os.environ['DOWNLOAD_SIGN_SECRET'] = 'offline-download-signing-only'

from fastapi.testclient import TestClient
from app.main import app, job_store
from app.models import Job, JobStatus, OutputMode


class DownloadContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / 'existing-translation.epub'
        self.content = b'PK offline existing translated book fixture'
        self.output.write_bytes(self.content)
        self.id = Path(self.tmp.name).name
        self.token = 'offline-job-access'
        self.session = 'offline-original-session'
        job_store.add(Job(
            id=self.id, trace_id='offline', source_filename='fixture.epub',
            input_path=str(self.output), output_path=str(self.output),
            output_mode=OutputMode.simplified, status=JobStatus.success,
            access_token=self.token, creator_session=self.session,
            token_expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        ))
        self.client = TestClient(app)
        self.headers = {'X-Job-Token': self.token, 'X-Client-Session': self.session}
        self.path = '/api/v2/jobs/' + self.id

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_refresh_recovers_existing_book_and_signed_download(self):
        listing = self.client.get('/api/v2/jobs', headers=self.headers)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.headers['cache-control'], 'no-store')
        self.assertTrue(any(j['job_id'] == self.id for j in listing.json()['items']))
        detail = self.client.get(self.path, headers=self.headers)
        self.assertEqual(detail.json()['status'], 'completed')
        self.assertEqual(detail.headers['cache-control'], 'no-store')
        downloaded = self.client.get(detail.json()['download_url'])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(hashlib.sha256(downloaded.content).digest(), hashlib.sha256(self.content).digest())

    def test_expired_link_can_be_recovered_with_existing_job_permission(self):
        stale = self.path + '/download?exp=1&sig=expired'
        self.assertEqual(self.client.get(stale).status_code, 403)
        self.assertEqual(self.client.get(stale, headers=self.headers).content, self.content)
        self.assertEqual(self.client.get(self.path).status_code, 403)
        refreshed = self.client.get(self.path, headers=self.headers)
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(self.client.get(refreshed.json()['download_url']).status_code, 200)

    def test_no_success_claim_when_existing_file_is_missing(self):
        self.output.unlink()
        self.assertEqual(self.client.get(self.path + '/download', headers=self.headers).status_code, 404)

    def test_frontend_and_admin_revalidation_preserves_security(self):
        for path in ['/', '/lib.js?v=release-test']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('must-revalidate', response.headers['cache-control'])
        response = self.client.get('/api/admin/orders')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')


class RuntimeBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'backend').mkdir()
        self.backup = self.root / 'release-backup'
        self.env = self.root / 'backend/.env'
        self.env.write_text('DATABASE_URL=sqlite:///./epub_jobs.db\n'
                            'OPENAI_MODEL=deepseek-v4-pro\n'
                            'OPENAI_API_KEY=offline-credential-to-preserve\n'
                            'DOWNLOAD_SIGN_SECRET=offline-secret-to-preserve\n'
                            'CONVERSION_PRICE_CNY=5.99\n')
        for filename in ['epub_jobs.db', 'translation_cache.db']:
            with sqlite3.connect(self.root / 'backend' / filename) as conn:
                conn.execute('CREATE TABLE fixture (id INTEGER PRIMARY KEY)')
                conn.execute('INSERT INTO fixture VALUES (1)')
        script = Path(__file__).resolve().parents[1] / 'scripts/deploy-server.sh'
        text = script.read_text().split('# Keep a consistent server-local runtime backup', 1)[1]
        self.source = text.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]

    def tearDown(self):
        self.tmp.cleanup()

    def run_backup(self):
        env = dict(os.environ)
        env.pop('DATABASE_URL', None)
        return subprocess.run([sys.executable, '-', str(self.root), str(self.backup)],
                              input=self.source, text=True, capture_output=True, env=env)

    def test_verified_database_cache_backup_and_credential_preservation(self):
        original = dotenv_values(self.env)
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = dotenv_values(self.env)
        for key in ['DATABASE_URL', 'OPENAI_API_KEY', 'DOWNLOAD_SIGN_SECRET']:
            self.assertEqual(updated[key], original[key])
        self.assertEqual(updated['CONVERSION_PRICE_CNY'], '1.99')
        self.assertEqual(updated['REPAIR_PRICE_CNY'], '0.99')
        self.assertEqual(updated['TRANSLATION_PRICE_PER_1K'], '0.05')
        self.assertEqual(updated['TRANSLATION_PRICE_300K_TO_1M_PER_1K'], '0.035')
        self.assertEqual(updated['TRANSLATION_PRICE_OVER_1M_PER_1K'], '0.025')
        self.assertEqual(updated['TRANSLATION_MIN_PRICE'], '3.99')
        self.assertEqual(updated['TRANSLATION_HIGH_QUALITY_MULTIPLIER'], '1.5')
        self.assertEqual(updated['TRANSLATION_LITERARY_MULTIPLIER'], '2')
        self.assertEqual(updated['TRANSLATION_PRO_MODEL_MULTIPLIER'], '3.4')
        self.assertEqual(dotenv_values(self.backup / 'production.env'), original)
        self.assertEqual(updated['OPENAI_MODEL'], 'deepseek-flash')
        for name in ['jobs.sqlite3', 'translation-cache.sqlite3']:
            with sqlite3.connect(self.backup / name) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM fixture').fetchone()[0], 1)
            self.assertEqual((self.backup / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(original['OPENAI_API_KEY'], result.stdout)

    def test_corrupt_cache_aborts_before_config_is_changed(self):
        before = self.env.read_bytes()
        (self.root / 'backend/translation_cache.db').write_bytes(b'not a database')
        self.assertNotEqual(self.run_backup().returncode, 0)
        self.assertEqual(self.env.read_bytes(), before)

    def test_custom_prices_and_fixed_translation_override_are_preserved(self):
        self.env.write_text(self.env.read_text().replace('CONVERSION_PRICE_CNY=5.99', 'CONVERSION_PRICE_CNY=0.02')
                            + 'REPAIR_PRICE_CNY=8.99\nTRANSLATION_PRICE_PER_1K=0.07\n'
                            + 'TRANSLATION_PRICE_300K_TO_1M_PER_1K=0.04\n'
                            + 'TRANSLATION_MIN_PRICE=4.49\nTRANSLATION_PRICE_CNY=5.99\n')
        self.assertEqual(self.run_backup().returncode, 0)
        values = dotenv_values(self.env)
        for key, expected in [('CONVERSION_PRICE_CNY', '0.02'), ('REPAIR_PRICE_CNY', '8.99'),
                              ('TRANSLATION_PRICE_PER_1K', '0.07'),
                              ('TRANSLATION_PRICE_300K_TO_1M_PER_1K', '0.04'),
                              ('TRANSLATION_MIN_PRICE', '4.49'), ('TRANSLATION_PRICE_CNY', '5.99')]:
            self.assertEqual(values[key], expected)

    def test_translation_migrates_only_approved_legacy_defaults(self):
        base = self.env.read_text()
        cases = (
            ('0.10', '5.99', '0.05', '3.99'),
            ('0.05', '3.99', '0.05', '3.99'),
            ('0.07', '4.49', '0.07', '4.49'),
            ('0.100', '5.990', '0.100', '5.990'),
        )
        for index, (rate, minimum, expected_rate, expected_minimum) in enumerate(cases):
            with self.subTest(rate=rate, minimum=minimum):
                self.env.write_text(base + f'TRANSLATION_PRICE_PER_1K={rate}\nTRANSLATION_MIN_PRICE={minimum}\n')
                self.backup = self.root / f'translation-price-backup-{index}'
                result = self.run_backup()
                self.assertEqual(result.returncode, 0, result.stderr)
                updated = dotenv_values(self.env)
                self.assertEqual(updated['TRANSLATION_PRICE_PER_1K'], expected_rate)
                self.assertEqual(updated['TRANSLATION_MIN_PRICE'], expected_minimum)
                self.assertEqual(updated['TRANSLATION_PRICE_300K_TO_1M_PER_1K'], '0.035')
                self.assertEqual(updated['TRANSLATION_PRICE_OVER_1M_PER_1K'], '0.025')

    def test_conversion_and_repair_migrate_only_their_approved_legacy_prices(self):
        base = self.env.read_text().replace('CONVERSION_PRICE_CNY=5.99\n', '')
        cases = (
            (None, '1.99', '0.99'),
            ('', '1.99', '0.99'),
            ("'   '", '1.99', '0.99'),
            ('5.99', '1.99', '0.99'),
            ('1.99', '1.99', '0.99'),
            ('0.99', '0.99', '0.99'),
            ('0.02', '0.02', '0.02'),
            ('8.99', '8.99', '8.99'),
            ('5.990', '5.990', '5.990'),
        )
        for index, (configured, conversion, repair) in enumerate(cases):
            with self.subTest(configured=configured):
                prices = '' if configured is None else (
                    f'CONVERSION_PRICE_CNY={configured}\nREPAIR_PRICE_CNY={configured}\n'
                )
                self.env.write_text(base + prices)
                original = dotenv_values(self.env)
                self.backup = self.root / f'price-backup-{index}'
                result = self.run_backup()
                self.assertEqual(result.returncode, 0, result.stderr)
                updated = dotenv_values(self.env)
                self.assertEqual(updated['CONVERSION_PRICE_CNY'], conversion)
                self.assertEqual(updated['REPAIR_PRICE_CNY'], repair)
                self.assertEqual(dotenv_values(self.backup / 'production.env'), original)

    def test_visibility_timeout_always_exceeds_custom_hard_limit(self):
        with self.env.open('a') as handle:
            handle.write('EPUB_BOOK_TIME_LIMIT=14000\n')
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(int(dotenv_values(self.env)['CELERY_VISIBILITY_TIMEOUT']), 14000)


class RepairPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = self.root / 'backend'
        self.backend.mkdir()
        self.env = self.backend / '.env'
        self.env.write_text('DATABASE_URL=sqlite:///./jobs.db\n')
        self.database = self.backend / 'jobs.db'
        with sqlite3.connect(self.database) as conn:
            conn.execute('CREATE TABLE epub_jobs (status TEXT)')
        self.default_repairs = self.root / 'default-repairs'
        script = Path(__file__).resolve().parents[1] / 'scripts/deploy-server.sh'
        body = script.read_text().split('check_jobs() {', 1)[1]
        self.source = body.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]

    def metadata(self, directory, status='paid'):
        path = directory / 'private-book-title' / 'order.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'status': status, 'filename': 'secret-book.epub'}))
        return path

    def run_preflight(self, **overrides):
        env = dict(os.environ)
        env.pop('DATABASE_URL', None)
        env.pop('REPAIR_UPLOAD_DIR', None)
        env.update(overrides)
        # Execute the deployed heredoc unchanged. Redirect only the default
        # directory lookup so tests never inspect another local repair task.
        prefix = (
            'import os\n'
            '_real_scandir = os.scandir\n'
            'def _offline_scandir(path):\n'
            "    if os.fspath(path) == '/tmp/epub-repair':\n"
            f'        path = {str(self.default_repairs)!r}\n'
            '    return _real_scandir(path)\n'
            'os.scandir = _offline_scandir\n'
        )
        before = self.database.read_bytes(), self.env.read_bytes()
        result = subprocess.run([sys.executable, '-', str(self.root)],
                                input=prefix + self.source, text=True, capture_output=True,
                                env=env, timeout=10)
        self.assertEqual((self.database.read_bytes(), self.env.read_bytes()), before)
        for private in ('private-book-title', 'secret-book.epub', 'private malformed content'):
            self.assertNotIn(private, result.stdout + result.stderr)
        return result

    def test_default_directory_active_repairs_block_deployment(self):
        for status in ('paid', 'pending', 'running'):
            with self.subTest(status=status):
                path = self.metadata(self.default_repairs, status)
                before = path.read_bytes()
                result = self.run_preflight()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('active repair', result.stderr)
                self.assertEqual(path.read_bytes(), before)

    def test_custom_directory_terminal_and_unpaid_repairs_allow_deployment(self):
        self.metadata(self.default_repairs, 'paid')
        custom = self.root / 'configured-repairs'
        self.env.write_text(self.env.read_text() + f'REPAIR_UPLOAD_DIR={custom}\n')
        for status in ('repaired', 'failed', 'pending_payment'):
            with self.subTest(status=status):
                path = self.metadata(custom, status)
                before = path.read_bytes()
                result = self.run_preflight()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(path.read_bytes(), before)

    def test_relative_directory_is_resolved_from_backend(self):
        self.metadata(self.backend / 'repair-runtime', 'paid')
        self.env.write_text(self.env.read_text() + 'REPAIR_UPLOAD_DIR=repair-runtime\n')
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('active repair', result.stderr)

    def test_environment_directory_overrides_dotenv(self):
        configured = self.root / 'configured-repairs'
        override = self.root / 'environment-repairs'
        self.metadata(configured, 'repaired')
        self.metadata(override, 'paid')
        self.env.write_text(self.env.read_text() + f'REPAIR_UPLOAD_DIR={configured}\n')
        result = self.run_preflight(REPAIR_UPLOAD_DIR=str(override))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('active repair', result.stderr)

    def test_missing_directory_and_legacy_directory_without_metadata_allow_deployment(self):
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        legacy = self.default_repairs / 'private-book-title'
        legacy.mkdir(parents=True)
        (legacy / 'source.epub').write_bytes(b'offline source fixture')
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((legacy / 'order.json').exists())

    def test_corrupt_or_unknown_metadata_aborts_without_disclosing_contents(self):
        path = self.metadata(self.default_repairs)
        for content in (b'private malformed content', b'\xff', b'[]', b'{}',
                        b'{"status": []}', b'{"status": "unknown"}'):
            with self.subTest(content=content):
                path.write_bytes(content)
                result = self.run_preflight()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Cannot verify repair metadata', result.stderr)
                self.assertEqual(path.read_bytes(), content)

    def test_metadata_must_be_a_regular_file(self):
        path = self.metadata(self.default_repairs)
        path.unlink()
        path.mkdir()
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot verify repair metadata', result.stderr)
        path.rmdir()
        target = self.root / 'external-metadata.json'
        target.write_text('{"status": "repaired"}')
        path.symlink_to(target)
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot verify repair metadata', result.stderr)

    def test_empty_directory_configuration_aborts(self):
        self.env.write_text(self.env.read_text() + 'REPAIR_UPLOAD_DIR=\n')
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot inspect repair directory', result.stderr)
        self.env.write_text('DATABASE_URL=sqlite:///./jobs.db\n'
                            f'REPAIR_UPLOAD_DIR={self.root / "configured-repairs"}\n')
        result = self.run_preflight(REPAIR_UPLOAD_DIR='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot inspect repair directory', result.stderr)

    def test_unreadable_repair_directory_aborts(self):
        self.default_repairs.write_bytes(b'not a directory')
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot inspect repair directory', result.stderr)

    def test_database_active_jobs_still_block_deployment(self):
        for status in ('pending', 'running'):
            with self.subTest(status=status):
                with sqlite3.connect(self.database) as conn:
                    conn.execute('DELETE FROM epub_jobs')
                    conn.execute('INSERT INTO epub_jobs VALUES (?)', (status,))
                result = self.run_preflight()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('queued/running jobs', result.stderr)


if __name__ == '__main__':
    unittest.main()
