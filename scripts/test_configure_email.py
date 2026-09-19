"""Offline tests: SMTP is replaced before any connection can be created."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import smtplib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from dotenv import dotenv_values as runtime_dotenv_values
except ImportError:
    runtime_dotenv_values = None


SCRIPT = Path(__file__).with_name("configure-email.py")
SPEC = importlib.util.spec_from_file_location("configure_email_script", SCRIPT)
configure = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = configure
SPEC.loader.exec_module(configure)


def dotenv_values(path):
    if runtime_dotenv_values is not None:
        return runtime_dotenv_values(path)
    return configure.parse_env(Path(path).read_text())[0]


class FakeSMTP:
    instances = []

    def __init__(self, *args, **kwargs):
        self.events = [("open", args, kwargs)]
        self.messages = []
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.events.append(("close",))

    def ehlo(self):
        self.events.append(("ehlo",))

    def starttls(self, **kwargs):
        self.events.append(("starttls", kwargs))

    def login(self, *args):
        self.events.append(("login", args))

    def send_message(self, message):
        self.messages.append(message)
        return {}


class ConfigureEmailTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = []
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / ".env"
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.contexts = contextlib.ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(contextlib.redirect_stdout(self.stdout))
        self.contexts.enter_context(contextlib.redirect_stderr(self.stderr))
        self.ssl_mock = self.contexts.enter_context(patch.object(configure.smtplib, "SMTP_SSL", FakeSMTP))
        self.plain_mock = self.contexts.enter_context(patch.object(configure.smtplib, "SMTP", FakeSMTP))

    def write_existing(self, security="ssl", enabled=True):
        values = {
            "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465" if security == "ssl" else "587",
            "SMTP_SECURITY": security, "SMTP_USER": "sender@example.com",
            "SMTP_PASSWORD": "secret-do-not-print", "SMTP_FROM": "sender@example.com",
            "SITE_BASE_URL": "https://fixepub.com", "NOTIFY_EMAIL_ENABLED": "1" if enabled else "0",
        }
        self.path.write_text("# unrelated comment\nDATABASE_URL='sqlite:///./jobs.db'\nUNRELATED='line 1\nline 2'\n"
                             + "\n".join(f"{key}='{value}'" for key, value in values.items()) + "\n")
        return values

    def run_script(self, *args):
        return configure.main(["--env", str(self.path), *args])

    def test_qq_preset_saves_0600_without_network_and_never_logs_addresses_or_password(self):
        self.path.write_text("# keep exact content\nDATABASE_URL='sqlite:///./jobs.db'\nMULTILINE='a\nb'\n")
        with patch.dict(os.environ, {"TEST_SMTP_AUTH": "auth-'quote-$literal"}):
            result = self.run_script("--qq", "--password-env", "TEST_SMTP_AUTH", "--non-interactive")
        self.assertEqual(result, 0, self.stderr.getvalue())
        self.assertEqual(FakeSMTP.instances, [])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        loaded = dotenv_values(self.path)
        self.assertEqual(loaded["SMTP_PASSWORD"], "auth-'quote-$literal")
        self.assertEqual(loaded["SMTP_HOST"], "smtp.qq.com")
        self.assertEqual(loaded["SMTP_PORT"], "465")
        self.assertEqual(loaded["SMTP_SECURITY"], "ssl")
        self.assertEqual(loaded["SMTP_FROM"], "249998620@qq.com")
        self.assertEqual(loaded["NOTIFY_EMAIL_ENABLED"], "1")
        self.assertEqual(loaded["OWNER_PAYMENT_EMAIL_ENABLED"], "1")
        self.assertEqual(loaded["OWNER_PAYMENT_EMAIL_TO"], "249998620@qq.com")
        self.assertIn("# keep exact content\nDATABASE_URL='sqlite:///./jobs.db'\nMULTILINE='a\nb'\n", self.path.read_text())
        for private in ("249998620", "auth-", "TEST_SMTP_AUTH"):
            self.assertNotIn(private, self.stdout.getvalue() + self.stderr.getvalue())

    def test_check_authenticates_over_tls_does_not_send_or_write(self):
        self.write_existing()
        original = self.path.read_bytes()
        self.assertEqual(self.run_script("--check"), 0, self.stderr.getvalue())
        self.assertEqual(self.path.read_bytes(), original)
        smtp = FakeSMTP.instances[0]
        self.assertEqual(smtp.messages, [])
        self.assertIn("context", smtp.events[0][2])
        self.assertEqual([e[0] for e in smtp.events], ["open", "ehlo", "login", "close"])

    def test_starttls_negotiated_before_authentication(self):
        self.write_existing(security="starttls")
        self.assertEqual(self.run_script("--check"), 0)
        events = [event[0] for event in FakeSMTP.instances[0].events]
        self.assertEqual(events, ["open", "ehlo", "starttls", "ehlo", "login", "close"])

    def test_only_explicit_test_to_sends_one_test_email(self):
        self.write_existing()
        original = self.path.read_bytes()
        self.assertEqual(self.run_script("--test-to", "recipient@example.com"), 0)
        messages = FakeSMTP.instances[0].messages
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["To"], "recipient@example.com")
        self.assertNotIn("secret-do-not-print", str(messages[0]))
        self.assertNotIn("recipient@example.com", self.stdout.getvalue())
        self.assertEqual(self.path.read_bytes(), original)

    def test_server_auth_errors_cannot_leak_credentials(self):
        self.write_existing()
        with patch.object(FakeSMTP, "login", side_effect=smtplib.SMTPAuthenticationError(
                535, b"secret-do-not-print sender@example.com")):
            self.assertEqual(self.run_script("--check"), 1)
        output = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertIn("认证失败", output)
        self.assertNotIn("secret-do-not-print", output)
        self.assertNotIn("sender@example.com", output)

    def test_tls_failure_does_not_fallback_or_send(self):
        self.write_existing(security="starttls")
        with patch.object(FakeSMTP, "starttls", side_effect=configure.ssl.SSLError("private diagnostic")):
            self.assertEqual(self.run_script("--test-to", "recipient@example.com"), 1)
        self.assertNotIn("login", [e[0] for e in FakeSMTP.instances[0].events])
        self.assertEqual(FakeSMTP.instances[0].messages, [])
        self.assertNotIn("private diagnostic", self.stderr.getvalue())

    def test_noninteractive_missing_config_fails_without_writing(self):
        self.assertEqual(self.run_script("--non-interactive"), 1)
        self.assertFalse(self.path.exists())
        self.assertEqual(FakeSMTP.instances, [])

    def test_switching_sender_does_not_reuse_previous_authorization_code(self):
        self.write_existing()
        original = self.path.read_bytes()
        self.assertEqual(self.run_script("--qq", "--non-interactive"), 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(FakeSMTP.instances, [])

    def test_password_dotenv_interpolation_is_rejected_before_saving(self):
        self.write_existing()
        original = self.path.read_bytes()
        with patch.dict(os.environ, {"TEST_AUTH": "${UNEXPECTED_SECRET}"}):
            self.assertEqual(self.run_script("--non-interactive", "--password-env", "TEST_AUTH"), 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn("UNEXPECTED_SECRET", self.stderr.getvalue())

    def test_disable_preserves_smtp_settings(self):
        self.write_existing()
        self.assertEqual(self.run_script("--non-interactive", "--disable"), 0)
        values = dotenv_values(self.path)
        self.assertEqual(values["NOTIFY_EMAIL_ENABLED"], "0")
        self.assertEqual(values["OWNER_PAYMENT_EMAIL_ENABLED"], "1")
        self.assertEqual(values["SMTP_PASSWORD"], "secret-do-not-print")
        self.assertEqual(values["UNRELATED"], "line 1\nline 2")
        self.assertEqual(FakeSMTP.instances, [])

    def test_qq_preset_preserves_existing_owner_recipient_and_disabled_switch(self):
        self.write_existing()
        with self.path.open("a") as handle:
            handle.write("OWNER_PAYMENT_EMAIL_TO='owner@another.example'\nOWNER_PAYMENT_EMAIL_ENABLED='0'\n")
        with patch.dict(os.environ, {"TEST_AUTH": "qq-offline-auth"}):
            self.assertEqual(self.run_script("--qq", "--address", "different@qq.com", "--non-interactive",
                                             "--password-env", "TEST_AUTH"), 0)
        values = dotenv_values(self.path)
        self.assertEqual(values["SMTP_FROM"], "different@qq.com")
        self.assertEqual(values["OWNER_PAYMENT_EMAIL_TO"], "owner@another.example")
        self.assertEqual(values["OWNER_PAYMENT_EMAIL_ENABLED"], "0")
        self.assertEqual(values["NOTIFY_EMAIL_ENABLED"], "1")
        self.assertEqual(values["UNRELATED"], "line 1\nline 2")
        self.assertEqual(FakeSMTP.instances, [])

    def test_explicit_owner_recipient_accepts_non_qq_and_does_not_change_sender(self):
        self.write_existing()
        self.assertEqual(self.run_script("--non-interactive", "--owner-to", "owner@another.example"), 0)
        values = dotenv_values(self.path)
        self.assertEqual(values["OWNER_PAYMENT_EMAIL_TO"], "owner@another.example")
        self.assertEqual(values["SMTP_FROM"], "sender@example.com")
        self.assertEqual(values["SMTP_PASSWORD"], "secret-do-not-print")
        self.assertNotIn("owner@another.example", self.stdout.getvalue() + self.stderr.getvalue())
        self.assertEqual(FakeSMTP.instances, [])

    def test_disable_owner_does_not_disable_customer_notifications(self):
        self.write_existing()
        self.assertEqual(self.run_script("--non-interactive", "--disable-owner-notifications"), 0)
        values = dotenv_values(self.path)
        self.assertEqual(values["NOTIFY_EMAIL_ENABLED"], "1")
        self.assertEqual(values["OWNER_PAYMENT_EMAIL_ENABLED"], "0")
        self.assertEqual(values["SMTP_PASSWORD"], "secret-do-not-print")
        self.assertEqual(FakeSMTP.instances, [])

    def test_enable_owner_does_not_enable_customer_notifications(self):
        self.write_existing(enabled=False)
        with self.path.open("a") as handle:
            handle.write("OWNER_PAYMENT_EMAIL_TO='owner@another.example'\nOWNER_PAYMENT_EMAIL_ENABLED='0'\n")
        self.assertEqual(self.run_script("--non-interactive", "--enable-owner-notifications"), 0)
        values = dotenv_values(self.path)
        self.assertEqual(values["NOTIFY_EMAIL_ENABLED"], "0")
        self.assertEqual(values["OWNER_PAYMENT_EMAIL_ENABLED"], "1")
        self.assertEqual(values["OWNER_PAYMENT_EMAIL_TO"], "owner@another.example")
        self.assertEqual(FakeSMTP.instances, [])

    def test_invalid_owner_email_cannot_be_written_or_leaked(self):
        self.write_existing()
        original = self.path.read_bytes()
        for address in ("not-an-email", "owner@example.com\nBcc: other@example.com", "${SECRET}@example.com",
                        ("a" * 65) + "@example.com"):
            with self.subTest(address=address):
                self.assertEqual(self.run_script("--non-interactive", "--owner-to", address), 1)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertNotIn(address, self.stdout.getvalue() + self.stderr.getvalue())
        self.assertEqual(FakeSMTP.instances, [])

    def test_invalid_existing_owner_switch_fails_without_overwriting(self):
        self.write_existing()
        with self.path.open("a") as handle:
            handle.write("OWNER_PAYMENT_EMAIL_TO='owner@example.com'\nOWNER_PAYMENT_EMAIL_ENABLED='unknown'\n")
        original = self.path.read_bytes()
        self.assertEqual(self.run_script("--non-interactive"), 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(FakeSMTP.instances, [])

    def test_check_does_not_save_owner_overrides_or_send_payment_email(self):
        self.write_existing(enabled=False)
        original = self.path.read_bytes()
        self.assertEqual(self.run_script("--check", "--owner-to", "owner@another.example",
                                         "--enable-owner-notifications"), 0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(FakeSMTP.instances[0].messages, [])
        self.assertNotIn("owner@another.example", self.stdout.getvalue() + self.stderr.getvalue())

    def test_credential_backslashes_and_quotes_roundtrip_without_corruption(self):
        self.write_existing()
        password = r"secret\\with\'quote\and\trail"
        with patch.dict(os.environ, {"TEST_AUTH": password}):
            self.assertEqual(self.run_script("--non-interactive", "--password-env", "TEST_AUTH"), 0)
        self.assertEqual(dotenv_values(self.path)["SMTP_PASSWORD"], password)

    def test_unrepresentable_trailing_backslash_never_overwrites_existing_env(self):
        self.write_existing()
        original = self.path.read_bytes()
        with patch.dict(os.environ, {"TEST_AUTH": "example" + "\\"}):
            self.assertEqual(self.run_script("--non-interactive", "--password-env", "TEST_AUTH"), 1)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_recipient_does_not_connect(self):
        self.write_existing()
        self.assertEqual(self.run_script("--test-to", "foo@example.com\nBcc: other@example.com"), 1)
        self.assertEqual(FakeSMTP.instances, [])

    def test_unsafe_site_url_and_mismatched_transport_are_rejected(self):
        self.write_existing()
        original = self.path.read_bytes()
        for url in ("http://fixepub.com", "https://localhost", "https://127.0.0.1", "https://10.0.0.1",
                    "https://user:password@example.com", "https://example.com/?secret=value", "https://example.com/path"):
            with self.subTest(url=url):
                self.assertEqual(self.run_script("--non-interactive", "--base-url", url), 1)
                self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.run_script("--non-interactive", "--security", "starttls"), 1)
        self.assertEqual(FakeSMTP.instances, [])

    def test_symlink_destination_is_not_replaced(self):
        self.write_existing()
        link = self.path.with_name("linked.env")
        link.symlink_to(self.path)
        original = self.path.read_bytes()
        self.assertEqual(configure.main(["--env", str(link), "--non-interactive"]), 1)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.read_bytes(), original)

    def test_standard_library_only_python_can_configure(self):
        env = dict(os.environ, TEST_AUTH="offline-test-auth")
        result = subprocess.run([sys.executable, "-S", str(SCRIPT), "--env", str(self.path),
                                 "--qq", "--non-interactive", "--password-env", "TEST_AUTH"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("offline-test-auth", result.stdout + result.stderr)
        self.assertEqual(dotenv_values(self.path)["SMTP_PASSWORD"], "offline-test-auth")
        self.assertEqual(dotenv_values(self.path)["OWNER_PAYMENT_EMAIL_TO"], "249998620@qq.com")
        self.assertEqual(dotenv_values(self.path)["OWNER_PAYMENT_EMAIL_ENABLED"], "1")

    def test_hidden_input_never_falls_back_to_visible_echo(self):
        self.write_existing()
        original = self.path.read_bytes()
        with patch.object(configure.sys.stdin, "isatty", return_value=True), patch.object(
                configure.getpass, "getpass", side_effect=configure.getpass.GetPassWarning("cannot hide input")):
            self.assertEqual(self.run_script(), 1)
        self.assertIn("不能隐藏输入", self.stderr.getvalue())
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipUnless(runtime_dotenv_values is not None, "后端环境安装 python-dotenv 后可执行真实加载器兼容检查")
    def test_saved_settings_roundtrip_with_actual_backend_dotenv_loader(self):
        self.write_existing()
        with patch.dict(os.environ, {"TEST_AUTH": r"example\\with\'quote"}):
            self.assertEqual(self.run_script("--non-interactive", "--password-env", "TEST_AUTH"), 0)
        self.assertEqual(runtime_dotenv_values(self.path)["SMTP_PASSWORD"], r"example\\with\'quote")


if __name__ == "__main__":
    unittest.main()
