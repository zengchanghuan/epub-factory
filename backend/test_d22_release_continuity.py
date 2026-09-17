"""Offline release checks: existing books remain discoverable and downloadable."""
import hashlib
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
        for key in ['DATABASE_URL', 'OPENAI_API_KEY', 'DOWNLOAD_SIGN_SECRET', 'CONVERSION_PRICE_CNY']:
            self.assertEqual(updated[key], original[key])
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

    def test_visibility_timeout_always_exceeds_custom_hard_limit(self):
        with self.env.open('a') as handle:
            handle.write('EPUB_BOOK_TIME_LIMIT=14000\n')
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(int(dotenv_values(self.env)['CELERY_VISIBILITY_TIMEOUT']), 14000)


if __name__ == '__main__':
    unittest.main()
