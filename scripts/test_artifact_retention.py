"""Synthetic offline contract tests; no application imports or real books/DB.

Every database and artifact belongs to one TemporaryDirectory. Network and
application imports are forbidden. Subprocess tests run only the stdlib CLI.
"""
from contextlib import ExitStack, redirect_stdout
import builtins
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("audit-artifact-retention.py")
SPEC = importlib.util.spec_from_file_location("retention_audit_test_target", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="retention-contract-"))).resolve()
        self.roots = {name: self.root / name for name in ("uploads", "outputs", "repair", "reduce_work")}
        for path in self.roots.values():
            path.mkdir()
        self.db = self.root / "backup.sqlite"
        self.connection = sqlite3.connect(self.db)
        self.addCleanup(self.connection.close)
        for table, columns in audit.TABLES.items():
            self.connection.execute('CREATE TABLE "' + table + '" (' + ','.join('"' + col + '" TEXT' for col in columns) + ')')
        self.connection.commit()
        self.real_import = builtins.__import__
        def restricted(name, *args, **kwargs):
            if name == "app" or name.startswith("app.") or name in {"dotenv", "requests", "redis"}:
                raise AssertionError("Application/config/network imports forbidden")
            return self.real_import(name, *args, **kwargs)
        self.stack.enter_context(patch("builtins.__import__", side_effect=restricted))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto", "socket.getaddrinfo"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("Network forbidden")))

    def insert(self, table, **values):
        columns = audit.TABLES[table]
        self.connection.execute('INSERT INTO "' + table + '" VALUES (' + ','.join('?' for _ in columns) + ')',
                                [values.get(column) for column in columns])
        self.connection.commit()

    def file(self, root, name, data=b"SYNTHETIC-BYTES-ONLY"):
        path = self.roots[root] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def job(self, key="book", status="success", **changes):
        source = self.file("uploads", key + ".epub")
        output = self.file("outputs", key + ".epub")
        value = dict(id=key, input_path=str(source), output_path=str(output), status=status, batch_id="",
                     translation_stats_json="{}", payment_entitlement_json="{}", payment_resolution_json="{}")
        value.update(changes)
        self.insert("epub_jobs", **value)
        return source, output

    def run_audit(self, **kwargs):
        return audit.audit(self.db, **self.roots, **kwargs)

    def entry(self, report, root, name):
        return next(row for row in report["files"] if (row["root"], row["relative_path"]) == (root, name))

    def state(self):
        return {str(path.relative_to(self.root)): (path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns,
                                                  hashlib.sha256(path.read_bytes()).hexdigest())
                for path in self.root.rglob('*') if path.is_file() and not path.is_symlink()}

    def test_success_preserves_every_file_and_never_authorizes_deletion(self):
        self.job()
        before = self.state()
        result = self.run_audit()
        self.assertEqual(before, self.state())
        self.assertTrue(result["read_only"])
        self.assertTrue(result["consistent_scan"])
        self.assertFalse(result["deletion_authorized"])
        self.assertIsNone(result["ttl_policy"])
        self.assertEqual(self.entry(result, "outputs", "book.epub")["classification"], "protected_reference")
        self.assertIn("completed_output_retained", self.entry(result, "outputs", "book.epub")["reasons"])
        self.assertEqual(result["database_sha256"], hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(result["missing_references"], [])

    def test_each_active_state_protects_unregistered_execution_and_prepared_directories(self):
        for index, status in enumerate(sorted(audit.ACTIVE)):
            self.job(str(index), status=status)
        self.file("outputs", ".execution-unregistered/work.epub")
        self.file("outputs", ".pdf-prepared-unregistered/book.epub")
        self.file("outputs", "unidentified.epub")
        result = self.run_audit()
        for name in (".execution-unregistered/work.epub", ".pdf-prepared-unregistered/book.epub", "unidentified.epub"):
            row = self.entry(result, "outputs", name)
            self.assertEqual(row["classification"], "unknown_protected")
            self.assertIn("active_writer_may_own_unregistered_files", row["reasons"])

    def test_payment_review_failed_and_expired_access_tokens_do_not_release_files(self):
        for index, resolution in enumerate(("paid", "paid_review", "external_refund_recorded")):
            self.job(str(index), status="failed", payment_resolution_json=json.dumps({"state": resolution}))
        self.job("test", status="failed", payment_entitlement_json=json.dumps({"state": "test_authorized"}))
        result = self.run_audit()
        for key in ("0", "1", "2", "test"):
            self.assertIn("payment_or_review_record_retained", self.entry(result, "outputs", key + ".epub")["reasons"])
        self.assertTrue(all(row["classification"] == "protected_reference" for row in result["files"]))

    def test_active_dispatch_execution_and_old_checkpoint_owner_are_retained(self):
        self.job(status="pending", translation_stats_json=json.dumps({"attempt_id": "new"}))
        self.insert("job_dispatch_outbox", job_id="book", attempt_id="new", status="sent")
        self.insert("job_executions", job_id="book", attempt_id="old", owner="private-owner", state="queued")
        prefix = "v2/" + hashlib.sha256(b"book").hexdigest() + "/attempts/old/owners/previous/chapter.json"
        self.file("reduce_work", prefix, b'{"content":"PRIVATE CHAPTER BODY"}')
        result = self.run_audit()
        row = self.entry(result, "reduce_work", prefix)
        self.assertEqual(row["classification"], "protected_reference")
        self.assertIn("known_job_checkpoints", row["reasons"])
        self.assertIn("active_dispatch_or_execution", row["reasons"])
        self.assertNotIn("PRIVATE CHAPTER BODY", json.dumps(result))
        self.assertNotIn("private-owner", json.dumps(result))

    def test_batch_review_receipt_and_derived_zip_are_not_orphans(self):
        self.job("child", status="failed", batch_id="batch")
        self.insert("admin_order_reviews", order_no="batch_batch", state="open")
        self.insert("order_funnel_events", order_no="batch_batch", event="payment_succeeded")
        self.file("outputs", "batch-batch.zip")
        result = self.run_audit()
        row = self.entry(result, "outputs", "batch-batch.zip")
        self.assertIn("batch_download_bundle", row["reasons"])
        self.assertIn("open_manual_review", row["reasons"])
        self.assertIn("recorded_payment_event", row["reasons"])
        self.assertEqual(row["classification"], "protected_reference")

    def test_review_prior_artifact_pointer_is_retained_even_without_current_job(self):
        old = self.file("outputs", "old.epub")
        missing = self.roots["outputs"] / "lost.epub"
        self.insert("admin_order_review_events", order_no="order-private", result_json=json.dumps({
            "prior_artifacts": {"old-job": str(old), "missing-job": str(missing)}, "note": "SECRET-NOTE"}))
        result = self.run_audit()
        self.assertIn("prior_review_artifact", self.entry(result, "outputs", "old.epub")["reasons"])
        self.assertEqual(result["missing_references"][0]["relative_path"], "lost.epub")
        self.assertNotIn("SECRET-NOTE", json.dumps(result))
        self.assertNotIn("old-job", json.dumps(result))

    def test_private_pdf_all_phases_keep_plan_assets_without_claiming_deliverability(self):
        identity = "a" * 32
        pdf = {"schema_version": 1, "product": "pdf_text_conversion", "phase": "prepared", "artifact_id": identity}
        self.job(status="awaiting_confirm", translation_stats_json=json.dumps({"pdf_conversion": pdf}))
        name = ".pdf-prepared-" + identity + "/book.epub"
        self.file("outputs", name)
        result = self.run_audit()
        row = self.entry(result, "outputs", name)
        self.assertEqual(row["classification"], "protected_reference")
        self.assertIn("pdf_prepared_artifact", row["reasons"])
        self.assertNotIn("download_verified", result)

    def test_missing_original_output_and_private_pdf_report_only_safe_relative_paths(self):
        pdf = {"schema_version": 1, "product": "pdf_text_conversion", "phase": "confirmed", "artifact_id": "b" * 32}
        source, output = self.job(translation_stats_json=json.dumps({"pdf_conversion": pdf}))
        source.unlink(); output.unlink()
        result = self.run_audit()
        self.assertEqual(len(result["missing_references"]), 4)
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertFalse(result["deletion_authorized"])

    def test_repair_original_paid_failed_and_legacy_delivered_file_are_retained(self):
        for index, status in enumerate(("paid", "failed", "repaired")):
            owner = str(index) * 32
            data = {"status": status, "filename": "original.epub", "payment_confirmed_at": 100,
                    "payment_confirmation_pending": True, "download_filename": "legacy_fixed.epub"}
            self.file("repair", owner + "/order.json", json.dumps(data).encode())
            self.file("repair", owner + "/original.epub")
            self.file("repair", owner + "/legacy_fixed.epub")
            self.file("repair", owner + "/.repair-unpublished.pending.epub")
        result = self.run_audit()
        for row in result["files"]:
            self.assertEqual(row["classification"], "protected_reference")
            self.assertIn("repair_paid_record_retained", row["reasons"])

    def test_repair_artifact_file_wins_over_download_label_and_missing_input_is_reported(self):
        owner = "a" * 32
        metadata = {"status": "repaired", "filename": "missing.epub", "artifact_file": ".repair-owner.epub",
                    "download_filename": "display_only.epub", "token": "SECRET-TOKEN", "qr_code": "SECRET-QR"}
        self.file("repair", owner + "/order.json", json.dumps(metadata).encode())
        self.file("repair", owner + "/.repair-owner.epub")
        result = self.run_audit()
        self.assertEqual([row["relative_path"] for row in result["missing_references"]], [owner + "/missing.epub"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("display_only", json.dumps(result))

    def test_malformed_json_unknown_status_or_schema_never_releases_unreferenced_files(self):
        self.job(status="NEW-UNKNOWN", translation_stats_json='{"duplicate":1,"duplicate":2}')
        self.file("outputs", "unowned.epub")
        self.file("repair", "a" * 32 + "/order.json", b"not json PRIVATE-CONTENT")
        result = self.run_audit()
        self.assertEqual(self.entry(result, "outputs", "unowned.epub")["classification"], "unknown_protected")
        self.assertNotIn("PRIVATE-CONTENT", json.dumps(result))
        self.assertIn("invalid_repair_metadata", [item["reason"] for item in result["issues"]])

    def test_missing_required_pointers_and_unknown_payment_pdf_metadata_fail_closed(self):
        cases = ({"input_path": ""}, {"output_path": None},
                 {"payment_resolution_json": '{"state":"future-state"}'},
                 {"payment_entitlement_json": '{"state":"future-state"}'},
                 {"translation_stats_json": '{"pdf_conversion":{"schema_version":2}}'})
        self.file("outputs", "unowned.epub")
        for index, values in enumerate(cases):
            self.job(str(index), **values)
        result = self.run_audit()
        self.assertEqual(self.entry(result, "outputs", "unowned.epub")["classification"], "unknown_protected")
        self.assertIn("missing_required_file_pointer", [item["reason"] for item in result["issues"]])

    def test_unreferenced_normal_file_is_review_only_but_private_dirs_are_always_unknown(self):
        self.file("outputs", "unowned.epub")
        self.file("outputs", ".execution-orphan/book.epub")
        self.file("outputs", ".pdf-prepared-orphan/book.epub")
        self.file("repair", "legacy-without-metadata/old.epub")
        result = self.run_audit()
        self.assertEqual(self.entry(result, "outputs", "unowned.epub")["classification"], "unreferenced_review")
        for row in result["files"]:
            self.assertNotIn(row["classification"], {"deletable", "delete", "expired"})
            if row["relative_path"] != "unowned.epub":
                self.assertEqual(row["classification"], "unknown_protected")

    def test_sensitive_runtime_files_are_metadata_only_never_parsed_or_hashed(self):
        for name in (".env", "secret.pem", "cache.db", "cache.db-wal", ".repair-locks/payment.lock", ".repair-executor.json"):
            self.file("repair", name, b"SECRET-CONFIG")
        original = audit._fingerprint_fd
        calls = []
        def fingerprint(fd, limit):
            calls.append(os.fstat(fd).st_ino)
            return original(fd, limit)
        with patch.object(audit, "_fingerprint_fd", side_effect=fingerprint):
            result = self.run_audit()
        for row in result["files"]:
            if row["kind"] == "file":
                self.assertIsNone(row["sha256"])
                self.assertEqual(row["classification"], "protected_infrastructure")
                self.assertNotIn(row["identity"][1], calls)
        self.assertNotIn("SECRET-CONFIG", json.dumps(result))

    def test_missing_required_table_column_and_unknown_table_refuse_whole_inventory(self):
        for sql in ("DROP TABLE job_executions", "ALTER TABLE epub_jobs RENAME COLUMN output_path TO unexpected",
                    "CREATE TABLE unknown_payment_system (id TEXT)"):
            with self.subTest(sql=sql):
                self.connection.execute("BEGIN")
                self.connection.execute(sql)
                self.connection.commit()
                with self.assertRaisesRegex(audit.AuditError, "unsupported_schema"):
                    self.run_audit()
                if sql.startswith("DROP"):
                    columns = audit.TABLES["job_executions"]
                    self.connection.execute("CREATE TABLE job_executions (" + ','.join(col + ' TEXT' for col in columns) + ')')
                elif sql.startswith("ALTER"):
                    self.connection.execute("ALTER TABLE epub_jobs RENAME COLUMN unexpected TO output_path")
                else:
                    self.connection.execute("DROP TABLE unknown_payment_system")
                self.connection.commit()

    def test_wal_shm_or_rollback_journal_sibling_reject_even_if_empty(self):
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.db) + suffix)
            sidecar.touch()
            try:
                with self.assertRaisesRegex(audit.AuditError, "database_sidecar_present"):
                    self.run_audit()
            finally:
                sidecar.unlink()

    def test_roots_database_and_nested_entries_do_not_follow_symlinks(self):
        target = self.file("uploads", "real.epub")
        link = self.roots["outputs"] / "link.epub"
        link.symlink_to(target)
        with self.assertRaises((audit.AuditError, OSError)):
            self.run_audit()
        link.unlink()
        alias = self.root / "root-link"
        alias.symlink_to(self.roots["uploads"], target_is_directory=True)
        with self.assertRaises((audit.AuditError, OSError)):
            audit.audit(self.db, **{**self.roots, "uploads": alias})
        db_link = self.root / "db-link"
        db_link.symlink_to(self.db)
        with self.assertRaises((audit.AuditError, OSError)):
            audit.audit(db_link, **self.roots)

    def test_fifo_rejected_without_opening_or_blocking(self):
        os.mkfifo(self.roots["outputs"] / "pipe")
        with self.assertRaisesRegex(audit.AuditError, "unsafe_entry"):
            self.run_audit()

    def test_overlapping_roots_and_database_inside_root_refuse(self):
        with self.assertRaisesRegex(audit.AuditError, "overlapping_roots"):
            audit.audit(self.db, **{**self.roots, "repair": self.roots["uploads"]})
        nested = self.roots["uploads"] / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(audit.AuditError, "overlapping_roots"):
            audit.audit(self.db, **{**self.roots, "repair": nested})
        moved = self.roots["uploads"] / "backup.sqlite"
        moved.write_bytes(self.db.read_bytes())
        with self.assertRaisesRegex(audit.AuditError, "database_inside_artifact_root"):
            audit.audit(moved, **self.roots)

    def test_external_and_relative_references_are_redacted_and_force_unknown(self):
        self.job(input_path="../SECRET-PATH/secret.epub", output_path="/SECRET-PATH/private.epub")
        self.file("outputs", "unowned.epub")
        result = self.run_audit()
        self.assertNotIn("SECRET-PATH", json.dumps(result))
        self.assertEqual(self.entry(result, "outputs", "unowned.epub")["classification"], "unknown_protected")
        self.assertEqual(len(result["issues"]), 2)

    def test_limits_reject_bool_unbounded_rows_files_and_total_bytes(self):
        self.job()
        for limits in (audit.Limits(max_entries=True), audit.Limits(max_depth=1000),
                       audit.Limits(max_rows=0), audit.Limits(max_entries=1),
                       audit.Limits(max_file_bytes=5), audit.Limits(max_total_bytes=5)):
            with self.subTest(limits=limits), self.assertRaises(audit.AuditError):
                self.run_audit(limits=limits)
        self.insert("admin_order_reviews", order_no="book", state="open")
        with self.assertRaisesRegex(audit.AuditError, "row_limit"):
            self.run_audit(limits=audit.Limits(max_rows=1))

    def test_file_directory_and_database_mutation_fail_instead_of_returning_stale_report(self):
        source, _ = self.job()
        original = audit._classify
        changes = (lambda: source.write_bytes(b"CHANGED"),
                   lambda: self.file("outputs", "new.epub"),
                   lambda: self.insert("admin_order_reviews", order_no="book", state="open"))
        for change in changes:
            def classify(*args):
                result = original(*args)
                change()
                return result
            with patch.object(audit, "_classify", side_effect=classify):
                with self.assertRaisesRegex(audit.AuditError, "source_changed"):
                    self.run_audit()

    def test_hardlink_never_becomes_unreferenced_candidate(self):
        source = self.file("uploads", "source.epub")
        os.link(source, self.roots["outputs"] / "linked.epub")
        result = self.run_audit()
        for row in result["files"]:
            self.assertEqual(row["classification"], "unknown_protected")
            self.assertIn("hardlink_requires_review", row["reasons"])

    def test_sqlite_is_immutable_read_only_and_no_sidecars_or_mutation_occur(self):
        original = sqlite3.connect
        calls = []
        def connect(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)
        before = self.state()
        with patch.object(audit.sqlite3, "connect", side_effect=connect):
            self.run_audit()
        self.assertEqual(len(calls), 1)
        self.assertIn("mode=ro&immutable=1", calls[0][0][0])
        self.assertTrue(calls[0][1]["uri"])
        self.assertEqual(before, self.state())

    def test_database_ancestor_aba_cannot_substitute_an_empty_reference_database(self):
        self.job()
        actual, replacement, parked = (self.root / name for name in ("actual-db", "replacement-db", "parked-db"))
        actual.mkdir(); replacement.mkdir()
        original_db = self.db
        self.db = actual / "backup.sqlite"
        self.db.write_bytes(original_db.read_bytes())
        alternate = replacement / "backup.sqlite"
        alternate.write_bytes(original_db.read_bytes())
        with sqlite3.connect(alternate) as conn:
            conn.execute("DELETE FROM epub_jobs")
        original_connect = sqlite3.connect
        calls = []
        def swap(*args, **kwargs):
            actual.rename(parked); replacement.rename(actual)
            try:
                calls.append(args[0])
                return original_connect(*args, **kwargs)
            finally:
                actual.rename(replacement); parked.rename(actual)
        before = self.state()
        with patch.object(audit.sqlite3, "connect", side_effect=swap):
            result = self.run_audit()
        self.assertEqual(before, self.state())
        self.assertEqual(len(calls), 1)
        self.assertNotIn(str(actual), calls[0])
        self.assertEqual(self.entry(result, "outputs", "book.epub")["classification"], "protected_reference")
        self.assertTrue(result["consistent_scan"])

    def test_repair_ancestor_aba_cannot_hide_paid_state_behind_restored_hashes(self):
        owner = "a" * 32
        self.file("repair", owner + "/order.json", b'{"status":"paid","filename":"source.epub"}')
        self.file("repair", owner + "/source.epub")
        self.file("outputs", "unregistered.epub")
        actual = self.roots["repair"]
        replacement, parked = self.root / "alternate-repair", self.root / "parked-repair"
        (replacement / owner).mkdir(parents=True)
        (replacement / owner / "order.json").write_bytes(b'{"status":"cancelled","filename":"source.epub"}')
        original_open = audit._open
        def swap(path, **kwargs):
            if Path(path) != actual / owner / "order.json":
                return original_open(path, **kwargs)
            actual.rename(parked); replacement.rename(actual)
            try:
                return original_open(path, **kwargs)
            finally:
                actual.rename(replacement); parked.rename(actual)
        before = self.state()
        with patch.object(audit, "_open", side_effect=swap):
            with self.assertRaisesRegex(audit.AuditError, "source_changed"):
                self.run_audit()
        self.assertEqual(before, self.state())

    def test_cli_is_stdlib_only_safe_json_and_requires_all_explicit_inputs(self):
        self.job()
        command = [sys.executable, "-I", "-B", str(SCRIPT), "--database", str(self.db)]
        for name, path in self.roots.items():
            command += ["--" + name.replace('_', '-'), str(path)]
        before = self.state()
        result = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                env={"PATH": os.environ.get("PATH", ""), "SECRET_CANARY": "DO-NOT-PRINT"})
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["deletion_authorized"])
        self.assertNotIn("DO-NOT-PRINT", result.stdout + result.stderr)
        self.assertEqual(before, self.state())
        missing = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT)], capture_output=True, text=True, timeout=10)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")

    def test_cli_error_does_not_expose_database_paths_or_partial_inventory(self):
        self.job()
        Path(str(self.db) + '-wal').touch()
        output = io.StringIO()
        args = ['--database', str(self.db)]
        for name, path in self.roots.items():
            args += ['--' + name.replace('_', '-'), str(path)]
        with redirect_stdout(output):
            code = audit.main(args)
        self.assertEqual(code, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result['error'], 'database_sidecar_present')
        self.assertFalse(result['consistent_scan'])
        self.assertFalse(result['deletion_authorized'])
        self.assertNotIn('files', result)
        self.assertNotIn(str(self.root), output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
