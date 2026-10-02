"""Offline privacy/read-only contracts for the deployment inventory script.

Temporary SQLite files are real. systemctl, /proc and Redis reads are controlled
at their external boundaries; every socket operation and application import is
forbidden. Test failures deliberately do not print private canary contents.
Run: backend/.venv/bin/python scripts/test_audit_production_config.py
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import builtins
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid


SCRIPT = Path(__file__).with_name("audit-production-config.py")
STATUSES = {"awaiting_confirm", "confirming", "pending_payment", "pending", "running", "success", "failed", "cancelled"}


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class AuditProductionConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fixepub-audit-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.backend = self.root / "backend"
        self.backend.mkdir()
        self.repair = self.root / "repair"
        self.repair.mkdir()
        self.db = self.backend / "jobs.db"
        self.env_file = self.backend / ".env"
        self.private = ["private_" + uuid.uuid4().hex for _ in range(8)]
        self.env = {
            "DATABASE_URL": "sqlite:///" + str(self.db),
            "REPAIR_UPLOAD_DIR": str(self.repair),
            "EPUB_TRANSLATION_CHECKPOINT_DB": str(self.backend / "checkpoints.db"),
            "JWT_SECRET": self.private[0], "OPENAI_API_KEY": self.private[1],
            "ALIPAY_PRIVATE_KEY": self.private[2], "SMTP_PASSWORD": self.private[3],
            "EPUB_LLM_RATE_LIMITER_ENABLED": "1", "EPUB_LLM_RATE_LIMIT_FAIL_OPEN": "0",
            "EPUB_LLM_RPM": "5", "EPUB_LLM_TPM": "10000",
        }
        self.live_env = {}
        self.commands, self.redis_calls, self.connections, self.statements = [], [], [], []
        self.violations = []
        self.clients = []
        self.redis_version = "6.0.16"
        self.redis_failure = None
        self.sqlite_failure = None
        self.sqlite_results = {}
        self.connect_sqlite = sqlite3.connect
        self.read_bytes = Path.read_bytes
        self.import_original = builtins.__import__
        self.previous_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.previous_cwd)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("builtins.__import__", self._import))
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo", "socket.socket.sendto"):
            self.stack.enter_context(patch(name, side_effect=self._forbidden))
        self.stack.enter_context(patch("subprocess.check_output", side_effect=self._systemctl))
        for name in ("subprocess.Popen", "subprocess.run", "subprocess.call", "os.system"):
            self.stack.enter_context(patch(name, side_effect=self._forbidden))
        spec = importlib.util.spec_from_file_location("auditor_" + uuid.uuid4().hex, SCRIPT)
        self.audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.audit)
        fake_redis = SimpleNamespace(Redis=SimpleNamespace(from_url=self._redis_client))
        self.stack.enter_context(patch.dict(sys.modules, {"redis": fake_redis}))
        # Route only the explicitly permitted read-only connection. Fixtures are
        # initialized through the saved original sqlite connect before main().
        self.stack.enter_context(patch.object(self.audit.sqlite3, "connect", side_effect=self._connect_readonly))

    def tearDown(self):
        self.assertEqual(self.violations, [], "forbidden import, database write, service mutation, or network attempted")

    def _forbidden(self, *args, **kwargs):
        self.violations.append("forbidden external action")
        raise AssertionError("external action forbidden in offline audit contract")

    def _import(self, name, *args, **kwargs):
        if name == "app" or name.startswith("app."):
            self.violations.append("application import")
            raise AssertionError("inventory must not import application modules")
        return self.import_original(name, *args, **kwargs)

    def _systemctl(self, command, **options):
        self.commands.append((command, options))
        if (not isinstance(command, list) or len(command) != 6
                or command[:2] != ["systemctl", "show"] or command[3] != "-p"
                or command[5] != "--value"
                or command[2] not in {"epub-factory", "epub-factory-worker", "epub-factory-housekeeping", "epub-factory-beat"}
                or command[4] not in {"MainPID", "LoadState", "ActiveState", "SubState"}
                or options != dict(text=True, stderr=subprocess.DEVNULL, timeout=10)):
            return self._forbidden()
        return {"MainPID": "654321\n", "LoadState": "loaded\n", "ActiveState": "active\n", "SubState": "running\n"}[command[4]]

    def _redis_client(self, url, **options):
        # URLs may contain private credentials: retain in memory for assertions,
        # never include them in failure messages or test output.
        self.redis_calls.append((url, options))
        if options != dict(socket_connect_timeout=3, socket_timeout=3):
            return self._forbidden()
        outer = self
        class Client:
            def __init__(self):
                self.events = []

            def ping(self):
                self.events.append("ping")
                if outer.redis_failure is not None:
                    raise outer.redis_failure
                return True

            def info(self, section):
                if section != "server":
                    return outer._forbidden()
                self.events.append("info:server")
                return {"redis_version": outer.redis_version, "ignored_private_field": outer.private[4]}

            def close(self):
                self.events.append("close")

            def __getattr__(self, _name):
                return outer._forbidden
        client = Client()
        self.clients.append(client)
        return client

    def _connect_readonly(self, address, **options):
        self.connections.append((address, options))
        expected = self.db.as_uri() + "?mode=ro"
        if address != expected or options != dict(uri=True, timeout=5):
            return self._forbidden()
        if self.sqlite_failure is not None:
            raise self.sqlite_failure
        connection = self.connect_sqlite(address, **options)
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
        def authorizer(action, one, two, _database, _source):
            if action == sqlite3.SQLITE_PRAGMA and one in {"quick_check", "journal_mode"} and two is None:
                return sqlite3.SQLITE_OK
            if action in allowed:
                return sqlite3.SQLITE_OK
            self.violations.append("database mutation attempted")
            return sqlite3.SQLITE_DENY
        connection.set_authorizer(authorizer)
        connection.set_trace_callback(self.statements.append)
        outer = self
        class ReadonlyConnection:
            def __enter__(self):
                connection.__enter__()
                return self

            def execute(self, statement):
                cursor = connection.execute(statement)
                # For diagnostic privacy cases, still execute the actual SQL
                # read first; only the driver result is replaced.
                if statement in outer.sqlite_results:
                    return _Cursor(outer.sqlite_results[statement])
                return cursor

            def __exit__(self, *args):
                try:
                    return connection.__exit__(*args)
                finally:
                    connection.close()
        return ReadonlyConnection()

    def prepare_db(self, statuses=("pending", "success")):
        with self.connect_sqlite(self.db) as connection:
            connection.execute("CREATE TABLE epub_jobs(status TEXT, source_filename TEXT)")
            connection.executemany("INSERT INTO epub_jobs VALUES (?, ?)", [(status, self.private[5]) for status in statuses])

    def snapshot(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def assert_private_absent(self, text):
        if any(value in text for value in self.private):
            self.fail("private canary reached the public inventory output")
        if any(url and url in text for url in self.env.values() if "://" in url and not url.startswith("sqlite:///")):
            self.fail("full connection URL reached the public inventory output")

    def run_audit(self):
        # A real dotenv parse, with a fake /proc fallback/override. No host .env
        # or process environment is read by this fixture.
        self.env_file.write_text("\n".join(f"{key}='{value}'" for key, value in self.env.items()) + "\n", encoding="utf-8")
        before = self.snapshot()
        stdout, stderr = io.StringIO(), io.StringIO()
        escaped_error = None
        def read_bytes(path):
            if str(path) == "/proc/654321/environ":
                return b"\0".join((key + "=" + value).encode() for key, value in self.live_env.items()) + b"\0"
            raise AssertionError("audit tried an unexpected raw file read")
        with patch("sys.argv", [str(SCRIPT), str(self.root)]), patch.object(Path, "read_bytes", read_bytes), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                self.audit.main()
            except Exception as exc:
                # A regression must not leak its private exception via the
                # unittest traceback while attempting to prove sanitization.
                escaped_error = type(exc).__name__
        combined = stdout.getvalue() + stderr.getvalue()
        self.assert_private_absent(combined)
        if escaped_error is not None:
            self.fail("inventory escaped its sanitized summary (" + escaped_error + ")")
        self.assertEqual(stderr.getvalue(), "", "inventory emitted unexpected diagnostic text")
        self.assertTrue(self.snapshot() == before, "audit changed its temporary source files")
        self.assertEqual(self.violations, [])
        result = json.loads(stdout.getvalue())
        self.assertFalse(result["application_imported"])
        self.assertFalse(result["backup_restore_verified"])
        self.assertEqual(len(self.commands), 14)
        return result

    def test_full_main_uses_only_readonly_sql_and_systemctl_reads_no_app_or_redis_writes(self):
        self.prepare_db(tuple(sorted(STATUSES)) + ("success", "success"))
        for key in ("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
            self.env[key] = "redis://reader:" + self.private[6] + "@127.0.0.1:16379/0"
        result = self.run_audit()
        self.assertEqual(result["database"]["dialect"], "sqlite")
        self.assertEqual(result["database"]["integrity_check"], "ok")
        self.assertEqual(result["database"]["journal_mode"], "delete")
        self.assertEqual(result["database"]["unknown_status_count"], 0)
        self.assertEqual(result["database"]["job_status_counts"], {**{name: 1 for name in STATUSES}, "success": 3})
        self.assertEqual(len(self.connections), 1)
        self.assertEqual([" ".join(sql.split()) for sql in self.statements], [
            "PRAGMA quick_check", "SELECT status, COUNT(*) FROM epub_jobs GROUP BY status", "PRAGMA journal_mode"])
        self.assertEqual(len(self.clients), 3)
        self.assertTrue(all(client.events == ["ping", "info:server", "close"] for client in self.clients))
        self.assertTrue(all(item["server_version"] == "6.0.16" for item in result["redis"].values()))
        self.assertTrue(result["jwt_configured_nonplaceholder"])
        self.assertEqual(result["rate_limiter"], {"enabled": True, "fail_open": False, "positive_rpm": True, "positive_tpm": True})

    def test_unknown_statuses_are_counted_without_echoing_book_or_private_text(self):
        self.prepare_db(("success", self.private[4], self.private[4], None, "future-status"))
        result = self.run_audit()
        self.assertEqual(result["database"]["job_status_counts"], {"success": 1})
        self.assertEqual(result["database"]["unknown_status_count"], 4)

    def test_mistyped_database_url_is_unknown_and_never_echoed_or_connected(self):
        self.env["DATABASE_URL"] = self.private[7]
        result = self.run_audit()
        self.assertEqual(result["database"], {"dialect": "unknown", "inventory_only": True})
        self.assertEqual(self.connections, [])
        self.assertFalse(self.db.exists())

    def test_postgres_credentials_are_not_exposed_and_no_database_connection_occurs(self):
        self.env["DATABASE_URL"] = "postgresql+psycopg://reader:" + self.private[6] + "@database.invalid/books"
        result = self.run_audit()
        self.assertEqual(result["database"], {"dialect": "postgresql", "inventory_only": True})
        self.assertEqual(self.connections, [])

    def test_unknown_redis_scheme_does_not_echo_input_or_construct_a_client(self):
        self.prepare_db()
        self.env["REDIS_URL"] = self.private[7] + "://reader:" + self.private[6] + "@redis.invalid/0"
        result = self.run_audit()
        self.assertEqual(result["redis"]["REDIS_URL"]["scheme"], "unknown")
        self.assertEqual(self.redis_calls, [])

    def test_malformed_redis_url_is_a_sanitized_error_not_a_raw_traceback(self):
        self.prepare_db()
        # Unbalanced brackets are invalid on both Python 3.10 and newer urllib;
        # checking the syntax must not depend on newer IPv6 host validation.
        self.env["REDIS_URL"] = "redis://[" + self.private[7] + "/0"
        try:
            result = self.run_audit()
        except Exception:
            self.fail("malformed Redis input escaped the sanitized summary")
        self.assertEqual(result["redis"]["REDIS_URL"]["scheme"], "unknown")
        self.assertEqual(result["redis"]["REDIS_URL"]["read_error_type"], "ValueError")
        self.assertEqual(self.redis_calls, [])

    def test_private_redis_exception_message_is_never_printed(self):
        self.prepare_db()
        self.env["REDIS_URL"] = "redis://reader:" + self.private[6] + "@redis.invalid/0"
        self.redis_failure = ConnectionError(self.private[4] + " " + self.env["REDIS_URL"])
        result = self.run_audit()
        self.assertEqual(result["redis"]["REDIS_URL"]["read_error_type"], "ConnectionError")
        self.assertNotIn("server_version", result["redis"]["REDIS_URL"])

    def test_private_sqlite_exception_message_is_never_printed(self):
        self.prepare_db()
        self.sqlite_failure = sqlite3.DatabaseError(self.private[4])
        result = self.run_audit()
        self.assertEqual(result["database"]["read_error_type"], "DatabaseError")

    def test_sqlite_diagnostics_are_fixed_labels_not_raw_engine_messages(self):
        self.prepare_db()
        self.sqlite_results = {"PRAGMA quick_check": [(self.private[4],)], "PRAGMA journal_mode": [(self.private[7],)]}
        result = self.run_audit()
        self.assertEqual(result["database"]["integrity_check"], "failed")
        self.assertEqual(result["database"]["journal_mode"], "unknown")

    def test_redis_version_allows_numeric_semver_only(self):
        self.prepare_db()
        self.env["REDIS_URL"] = "redis://127.0.0.1:16379/0"
        for value, expected in (("6.0.16", "6.0.16"), ("7.2.10", "7.2.10"),
                                (self.private[7], "unknown"), ("7.2.10\n" + self.private[4], "unknown"),
                                (None, "unknown"), ({"private": self.private[4]}, "unknown")):
            with self.subTest(valid=isinstance(value, str) and value in {"6.0.16", "7.2.10"}):
                self.redis_version = value
                self.commands.clear()
                result = self.run_audit()
                self.assertEqual(result["redis"]["REDIS_URL"]["server_version"], expected)

    def test_live_environment_override_is_compared_without_printing_values(self):
        self.prepare_db()
        self.env["JWT_SECRET"] = "CHANGE_ME_IN_PRODUCTION_PLEASE"
        self.live_env = {"JWT_SECRET": self.private[0], "EPUB_LLM_RPM": "invalid", "EPUB_LLM_TPM": "0"}
        result = self.run_audit()
        self.assertEqual(result["config_source"], "process-environment-with-dotenv-fallback")
        self.assertTrue(result["jwt_configured_nonplaceholder"])
        self.assertFalse(result["rate_limiter"]["positive_rpm"])
        self.assertFalse(result["rate_limiter"]["positive_tpm"])
        self.assertTrue(all(result["api_worker_config_equal"].values()))

    def test_repair_inventory_reports_counts_without_names_or_metadata_contents(self):
        self.prepare_db()
        for number, status in enumerate(("paid", "paid", "repaired", self.private[7])):
            directory = self.repair / (self.private[5] + str(number))
            directory.mkdir()
            (directory / "order.json").write_text(json.dumps({"status": status, "filename": self.private[5], "email": self.private[4]}))
        (self.repair / "legacy-directory").mkdir()
        result = self.run_audit()
        inventory = result["repair_inventory"]
        self.assertEqual(inventory["persisted_status_counts"], {"paid": 2, "repaired": 1})
        self.assertEqual(inventory["unreadable_metadata"], 1)
        self.assertEqual(inventory["directories_without_metadata"], 1)
        self.assertFalse(inventory["legacy_memory_state_verified"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
