"""Offline configuration regressions using generated, disposable RSA keys."""
import base64
import contextlib
import importlib.util
import io
from pathlib import Path
import stat
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from dotenv import dotenv_values

spec = importlib.util.spec_from_file_location("configure_alipay_key", Path(__file__).with_name("configure-alipay-public-key.py"))
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


def public_pem(key):
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


class ConfigureAlipayKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.provider = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / ".env"
        self.private = self.app.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        self.original = ("# keep comments\nSMTP_PASSWORD='do-not-print'\nMULTILINE='one\ntwo'\n"
                         "ALIPAY_APP_ID=offline\nALIPAY_PRIVATE_KEY='" + base64.b64encode(self.private).decode() + "'\n"
                         "ALIPAY_PUBLIC_KEY='wrong-old-value'\n")
        self.path.write_text(self.original)

    def test_accepts_pem_and_base64_public_key(self):
        existing = dotenv_values(self.path)
        normalized = configure.validate_key(public_pem(self.provider), existing)
        self.assertEqual(configure.validate_key(normalized, existing), normalized)
        self.assertEqual(configure.validate_key(public_pem(self.provider).replace('\n', '\\n'), existing), normalized)

    def test_private_key_cannot_be_mistaken_for_public_key(self):
        private_pem = self.app.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        for value in [private_pem, base64.b64encode(self.private).decode()]:
            with self.subTest(format='PEM' if value.startswith('-') else 'DER'), self.assertRaises(configure.ConfigurationError):
                configure.save_key(self.path, value)
        self.assertEqual(self.path.read_text(), self.original)

    def test_rejects_app_public_key_and_preserves_file(self):
        with self.assertRaisesRegex(configure.ConfigurationError, '应用公钥'):
            configure.save_key(self.path, public_pem(self.app))
        self.assertEqual(self.path.read_text(), self.original)

    def test_rejects_non_rsa_and_short_key(self):
        for key in [ec.generate_private_key(ec.SECP256R1()), rsa.generate_private_key(public_exponent=65537, key_size=1024)]:
            with self.assertRaises(configure.ConfigurationError):
                configure.save_key(self.path, public_pem(key))

    def test_save_only_changes_public_key_and_backs_up_privately(self):
        before = dotenv_values(self.path, interpolate=False)
        configure.save_key(self.path, public_pem(self.provider))
        after = dotenv_values(self.path, interpolate=False)
        self.assertEqual({k:v for k,v in before.items() if k!='ALIPAY_PUBLIC_KEY'},
                         {k:v for k,v in after.items() if k!='ALIPAY_PUBLIC_KEY'})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        backup_dir = self.path.parent / '.config-backups'
        self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
        backups = list(backup_dir.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertIn('# keep comments', self.path.read_text())

    def test_check_is_read_only_and_never_prints_secret(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = configure.main(['--env',str(self.path),'--check'])
        self.assertEqual(result, 2)
        self.assertEqual(self.path.read_text(), self.original)
        for secret in ['wrong-old-value', 'do-not-print', base64.b64encode(self.private).decode()]:
            self.assertNotIn(secret, out.getvalue()+err.getvalue())

    def test_public_key_file_does_not_print_key(self):
        key_file = self.path.parent / 'public-key.pem'
        key_file.write_text(public_pem(self.provider))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = configure.main(['--env',str(self.path),'--public-key-file',str(key_file)])
            check = configure.main(['--env',str(self.path),'--check'])
        self.assertEqual((result,check),(0,0))
        self.assertNotIn(public_pem(self.provider),out.getvalue())
        self.assertIn('尚未联网验证',out.getvalue())

    def test_symlink_env_and_backup_directory_are_rejected(self):
        alias = self.path.parent / 'alias.env'
        alias.symlink_to(self.path)
        with self.assertRaises(configure.ConfigurationError):
            configure.save_key(alias,public_pem(self.provider))
        target=self.path.parent/'elsewhere'; target.mkdir()
        (self.path.parent/'.config-backups').symlink_to(target)
        with self.assertRaises(configure.ConfigurationError):
            configure.save_key(self.path,public_pem(self.provider))
        self.assertEqual(self.path.read_text(),self.original)


if __name__ == '__main__':
    unittest.main()
