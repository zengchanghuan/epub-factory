import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("migration", Path(__file__).with_name("migrate-legacy-repair.py"))
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class LegacyRepairMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.directory = self.project / "repairs"
        self.job_id = "a" * 32
        self.job_dir = self.directory / self.job_id
        self.job_dir.mkdir(parents=True)
        self.source = self.job_dir / "original.epub"
        self.source.write_bytes(b"offline fixture, never sent to a gateway")
        self.response = {"job_id": self.job_id, "status": "pending_payment", "error": None, "download_filename": None}
        self.status = patch.object(migration, "get_status", return_value=self.response).start()
        self.quiet = patch.object(migration, "check_quiet", return_value="123").start()
        self.addCleanup(patch.stopall)

    def run_migration(self, apply=False):
        return migration.preserve(self.project, self.directory, self.job_id, {"effective_old_price": "5.99"}, apply)

    def test_preview_never_writes(self):
        before = set(self.project.rglob("*"))
        result = self.run_migration()
        self.assertEqual(result["mode"], "preview")
        self.assertEqual(set(self.project.rglob("*")), before)
        self.quiet.assert_not_called()

    def test_apply_preserves_pending_amount_identity_and_source(self):
        digest = migration.file_hash(self.source)
        result = self.run_migration(True)
        metadata = self.job_dir / "order.json"
        saved = json.loads(metadata.read_text())
        self.assertEqual(saved, {"status": "pending_payment", "filename": "original.epub",
                                 "quoted_amount": "5.99", "expected_amount": "5.99",
                                 "out_trade_no": "repair_" + self.job_id, "is_test_order": False})
        self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
        snapshot_path = Path(result["snapshot_path"])
        self.assertEqual(stat.S_IMODE(snapshot_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snapshot_path.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(snapshot_path.read_text())["source_sha256"], digest)
        self.assertEqual(migration.file_hash(self.source), digest)
        self.quiet.assert_called_once()

    def test_existing_metadata_is_never_overwritten(self):
        path = self.job_dir / "order.json"
        path.write_text('{"status":"paid"}')
        with self.assertRaises(migration.Refused):
            self.run_migration(True)
        self.assertEqual(path.read_text(), '{"status":"paid"}')

    def test_concurrent_metadata_creation_is_not_overwritten(self):
        original = migration.create_private_json
        def interleave(path, value):
            original(path, value)
            if path.name.startswith(".migration-"):
                (self.job_dir / "order.json").write_text('{"status":"paid"}')
        with patch.object(migration, "create_private_json", side_effect=interleave):
            with self.assertRaises(FileExistsError):
                self.run_migration(True)
        self.assertEqual(json.loads((self.job_dir / "order.json").read_text())["status"], "paid")

    def test_final_nonpending_states_are_rejected(self):
        for status in ("paid", "repaired", "failed", "unknown"):
            with self.subTest(status=status):
                self.response["status"] = status
                with self.assertRaises(migration.Refused):
                    self.run_migration(True)
        self.assertFalse((self.job_dir / "order.json").exists())

    def test_other_live_memory_order_is_rejected(self):
        (self.directory / ("b" * 32)).mkdir()
        with self.assertRaises(migration.Refused):
            self.run_migration(True)

    def test_old_expired_directory_can_be_skipped(self):
        (self.directory / ("b" * 32)).mkdir()
        self.status.side_effect = lambda job_id: self.response if job_id == self.job_id else None
        self.assertEqual(self.run_migration(True)["mode"], "applied")

    def test_two_sources_and_completed_output_are_rejected(self):
        extra = self.job_dir / "second.epub"
        extra.write_bytes(b"second")
        with self.assertRaises(migration.Refused):
            self.run_migration(True)
        extra.unlink()
        (self.job_dir / "original_fixed.epub").write_bytes(b"output")
        with self.assertRaises(migration.Refused):
            self.run_migration(True)

    def test_symlink_source_is_rejected(self):
        self.source.unlink()
        self.source.symlink_to(self.project / "missing")
        with self.assertRaises(migration.Refused):
            self.run_migration(True)

    def test_open_ingress_refuses_without_writes(self):
        self.quiet.side_effect = migration.Refused("nginx is running")
        with self.assertRaises(migration.Refused):
            self.run_migration(True)
        self.assertFalse((self.job_dir / "order.json").exists())

    def test_cli_requires_explicit_confirmation(self):
        with patch.object(migration, "read_server") as server:
            self.assertEqual(migration.main(["--project-dir", str(self.project), "--job-id", self.job_id]), 1)
            server.assert_not_called()


class ServerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.main = self.project / "backend/app/main.py"
        self.main.parent.mkdir(parents=True)
        self.main.write_text("reviewed legacy source fixture")
        self.env = self.project / "backend/.env"
        self.env.write_text("")
        for path in (self.main, self.env):
            os.utime(path, (999, 999))
        self.repairs = self.project / "repairs"
        self.repairs.mkdir()
        self.config = {"REPAIR_UPLOAD_DIR": str(self.repairs)}
        self.proc = self.project / "proc"
        self.pid = self.proc / "42"
        (self.pid / "task/42").mkdir(parents=True)
        (self.pid / "task/42/children").write_text("")
        self.argv = ["/venv/bin/python", "/venv/bin/uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
        self.set_argv(self.argv)
        (self.pid / "environ").write_bytes(b"UNRELATED_SECRET=do-not-log\0")
        (self.proc / "stat").write_text("btime 1000\n")
        (self.pid / "stat").write_text("42 (python) " + " ".join(["0"] * 19 + ["100"]))
        self.stack = __import__("contextlib").ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(migration, "LEGACY_MAIN_SHA256", migration.file_hash(self.main)))
        self.stack.enter_context(patch.object(migration, "command", return_value="42"))
        self.stack.enter_context(patch.object(migration.os, "sysconf", return_value=100))
        self.stack.enter_context(patch.dict("sys.modules", {"dotenv": types.SimpleNamespace(dotenv_values=lambda *a, **k: self.config)}))

    def set_argv(self, argv):
        (self.pid / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")

    def read(self, confirmed=False):
        return migration.read_server(self.project, False, confirmed, self.proc)

    def test_absolute_uvicorn_argv_and_whitelist_only(self):
        directory, evidence = self.read()
        self.assertEqual(directory, self.repairs)
        self.assertEqual(evidence["effective_old_price"], "5.99")
        self.assertNotIn("do-not-log", json.dumps(evidence))
        self.set_argv(["/venv/bin/python", "-m", "uvicorn"] + self.argv[2:])
        self.read()

    def test_wrong_sha_or_newer_source_refuses(self):
        self.main.write_text("unexpected")
        with self.assertRaises(migration.Refused):
            self.read()
        self.main.write_text("reviewed legacy source fixture")
        with self.assertRaises(migration.Refused):
            self.read()

    def test_inherited_and_dotenv_price_mismatch_refuse(self):
        self.config["REPAIR_PRICE_CNY"] = "0.99"
        with self.assertRaises(migration.Refused):
            self.read()
        self.config.pop("REPAIR_PRICE_CNY")
        (self.pid / "environ").write_bytes(b"REPAIR_PRICE_CNY=1.99\0")
        with self.assertRaises(migration.Refused):
            self.read()

    def test_smtp_only_changed_env_requires_explicit_evidence(self):
        os.utime(self.env, (2000, 2000))
        with self.assertRaises(migration.Refused):
            self.read()
        _, evidence = self.read(True)
        self.assertTrue(evidence["price_unchanged_since_start_confirmation"])

    def test_nonloopback_multiple_workers_and_payment_bypass_refuse(self):
        for argv in (self.argv + ["--workers", "2"], [part.replace("127.0.0.1", "0.0.0.0") for part in self.argv]):
            self.set_argv(argv)
            with self.assertRaises(migration.Refused):
                self.read()
        self.set_argv(self.argv)
        self.config["SKIP_PAYMENT_CHECK"] = "true"
        with self.assertRaises(migration.Refused):
            self.read()


class QuietIngressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        (self.proc / "net").mkdir()
        (self.proc / "net/tcp6").write_text("header\n")
        self.listener = "0: 0100007F:1F40 00000000:0000 0A 0:0 0:0 0 1000 0 123"
        (self.proc / "net/tcp").write_text("header\n" + self.listener + "\n")
        self.stack = __import__("contextlib").ExitStack()
        self.addCleanup(self.stack.close)
        self.command = self.stack.enter_context(patch.object(migration, "command", return_value="inactive"))
        self.pgrep = self.stack.enter_context(patch.object(migration.subprocess, "run", return_value=types.SimpleNamespace(returncode=1)))

    def test_stopped_nginx_and_loopback_listener(self):
        self.assertEqual(migration.check_quiet(self.proc, 0), "123")

    def test_running_nginx_or_worker_process_refuses(self):
        self.command.return_value = "active"
        with self.assertRaises(migration.Refused):
            migration.check_quiet(self.proc, 0)
        self.command.return_value = "inactive"
        self.pgrep.return_value.returncode = 0
        with self.assertRaises(migration.Refused):
            migration.check_quiet(self.proc, 0)

    def test_wildcard_listener_and_inflight_connections_refuse(self):
        (self.proc / "net/tcp").write_text("header\n" + self.listener.replace("0100007F", "00000000") + "\n")
        with self.assertRaises(migration.Refused):
            migration.check_quiet(self.proc, 0)
        (self.proc / "net/tcp").write_text("header\n" + self.listener + "\n" + self.listener.replace(" 0A ", " 01 ") + "\n")
        with self.assertRaises(migration.Refused):
            migration.check_quiet(self.proc, 0)


if __name__ == "__main__":
    unittest.main()
