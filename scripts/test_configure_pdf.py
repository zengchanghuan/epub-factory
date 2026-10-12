"""Offline tests: fake secrets and simulated HTTP only."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from dotenv import dotenv_values

spec = importlib.util.spec_from_file_location('configure_pdf', Path(__file__).with_name('configure-pdf.py'))
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)
FAKE_KEY = 'fake-token-never-send-1234'


class ConfigurePdfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '.env'
        self.original = '# retain comment\nALIPAY_PRIVATE_KEY="line1\nline2"\nSMTP_PASSWORD=keep-me\nPDF_TRANSLATION_ENABLED=0\n'
        self.path.write_text(self.original)
        self.path.chmod(0o600)

    def test_private_atomic_save_preserves_settings_and_backup(self):
        configure.save(self.path, FAKE_KEY, priority_pages=800)
        values = dotenv_values(self.path)
        self.assertEqual(values['MINERU_API_KEY'], FAKE_KEY)
        self.assertEqual(values['PDF_TRANSLATION_PRICE_PER_PAGE_CNY'], '0.20')
        self.assertEqual(values['PDF_TRANSLATION_MIN_PRICE_CNY'], '3.99')
        self.assertEqual(values['MINERU_LOCAL_PRIORITY_PAGES'], '800')
        self.assertEqual(values['PDF_TRANSLATION_ENABLED'], '0')
        self.assertEqual(values['ALIPAY_PRIVATE_KEY'], 'line1\nline2')
        self.assertEqual(values['SMTP_PASSWORD'], 'keep-me')
        self.assertIn('# retain comment', self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        backups = list(self.path.parent.glob('.config-backups/*.env'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_rotation_preserves_allocation_account_and_switch(self):
        self.path.write_text(self.original + 'MINERU_LOCAL_PRIORITY_PAGES=700\nMINERU_QUOTA_ACCOUNT=stable\n')
        configure.save(self.path, FAKE_KEY)
        values = dotenv_values(self.path)
        self.assertEqual(values['MINERU_LOCAL_PRIORITY_PAGES'], '700')
        self.assertEqual(values['MINERU_QUOTA_ACCOUNT'], 'stable')
        self.assertEqual(values['PDF_TRANSLATION_ENABLED'], '0')

    def test_reject_invalid_values_without_modifying_original(self):
        for options in ({'key': 'invalid\nKEY=value'}, {'key': FAKE_KEY, 'rate': 'NaN'},
                        {'key': FAKE_KEY, 'minimum': '-1'}, {'key': FAKE_KEY, 'priority_pages': 1001}):
            with self.assertRaises(configure.ConfigurationError):
                configure.save(self.path, **options)
            self.assertEqual(self.path.read_text(), self.original)

    def test_reject_symlink_and_public_source(self):
        link = self.path.parent / 'link'
        link.symlink_to(self.path)
        with self.assertRaises(configure.ConfigurationError): configure.read_private(link)
        self.path.chmod(0o644)
        with self.assertRaises(configure.ConfigurationError): configure.read_private(self.path)

    def test_cli_source_alias_and_no_secret_output(self):
        source = self.path.parent / 'source.env'
        source.write_text('MINERU_API_TOKEN=' + FAKE_KEY)
        source.chmod(0o600)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(configure.main(['--env', str(self.path), '--key-env-file', str(source), '--priority-pages', '800']), 0)
            self.assertEqual(configure.main(['--env', str(self.path), '--check']), 0)
        self.assertNotIn(FAKE_KEY, output.getvalue())

    def test_readonly_auth_requires_differential_evidence(self):
        original_client = httpx.Client
        for good, control_status, expected in [(True, 200, None), (True, 401, None), (False, 401, configure.ConfigurationError)]:
            requests = []
            def handler(request):
                requests.append(request)
                if good and request.headers['authorization'] == 'Bearer ' + FAKE_KEY:
                    return httpx.Response(200, json={'code': '-60012'})
                return httpx.Response(control_status, json={'message': 'Unauthorized'} if control_status == 401 else {'code': 'A0202'})
            with patch('httpx.Client', side_effect=lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw)):
                if expected:
                    with self.assertRaises(expected): configure.check_auth(FAKE_KEY)
                else: configure.check_auth(FAKE_KEY)
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(r.method == 'GET' and r.url.host == 'mineru.net' for r in requests))


if __name__ == '__main__':
    unittest.main()
