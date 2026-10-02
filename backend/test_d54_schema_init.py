"""Real local SQLite startup/upgrade races; no app startup or external I/O."""
from contextlib import ExitStack
import fcntl
import multiprocessing
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError

from app import storage_db as db


def _network_guard(stack):
    guards = []
    for target in ("connect", "connect_ex"):
        guards.append(stack.enter_context(patch.object(socket.socket, target,
                          side_effect=AssertionError("external network forbidden"))))
    guards.append(stack.enter_context(patch.object(socket, "getaddrinfo",
                          side_effect=AssertionError("external DNS forbidden"))))
    return guards


def _schema_worker(url, barrier, results, kind):
    with ExitStack() as stack:
        guards = _network_guard(stack)
        stack.enter_context(patch.dict(os.environ, {"DATABASE_URL": url}))
        engine = None
        original_create_engine = create_engine
        def slow_engine(*args, **kwargs):
            target = original_create_engine(*args, **kwargs)
            # Widen the actual read-before-DDL window; no schema/DDL is mocked.
            @event.listens_for(target, "after_cursor_execute")
            def slow_schema_read(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("PRAGMA") and ("table_info" in statement or "table_xinfo" in statement):
                    time.sleep(0.015)
            return target
        stack.enter_context(patch.object(db, "create_engine", slow_engine))
        try:
            barrier.wait(timeout=15)
            if kind == "direct":
                engine = slow_engine(url)
                db._ensure_compatible_schema(engine)
            else:
                engine = db._make_engine()
            info = inspect(engine)
            results.put({"ok": True, "tables": sorted(info.get_table_names()),
                         "job_columns": sorted(c["name"] for c in info.get_columns("epub_jobs")),
                         "chunk_columns": sorted(c["name"] for c in info.get_columns("job_chunks")),
                         "indexes": sorted(i["name"] for i in info.get_indexes("notifications"))})
        except BaseException as exc:
            results.put({"ok": False, "type": type(exc).__name__, "message": str(exc)})
        finally:
            if engine is not None:
                engine.dispose()
            for guard in guards:
                guard.assert_not_called()


def _hold_schema(url, ready):
    with ExitStack() as stack:
        _network_guard(stack)
        engine = create_engine(url)
        with db._sqlite_schema_lock(engine):
            ready.set()
            # Intentionally killed by the test; OS must release the lock.
            time.sleep(60)


class SchemaInitializationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.guards = _network_guard(self.stack)
        self.addCleanup(lambda: [guard.assert_not_called() for guard in self.guards])
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="epub-schema-"))).resolve()
        self.path = self.root / "jobs.db"
        self.url = "sqlite:///" + str(self.path)
        self.stack.enter_context(patch.dict(os.environ, {"DATABASE_URL": self.url}))

    def engine(self, url=None):
        result = create_engine(url or self.url)
        self.stack.callback(result.dispose)
        return result

    def initialize(self):
        result = db._make_engine()
        self.stack.callback(result.dispose)
        return result

    def race(self, kinds):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(len(kinds))
        results = context.Queue()
        processes = [context.Process(target=_schema_worker, args=(self.url, barrier, results, kind)) for kind in kinds]
        try:
            for process in processes: process.start()
            rows = [results.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(row["ok"] for row in rows), rows)
            return rows
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate(); process.join(timeout=5)
            results.close(); results.join_thread()

    def make_legacy(self):
        # Keep an actual old job/chunk and an R13 notification while dropping
        # additive columns/tables/indexes. No modern create_all during the race.
        with sqlite3.connect(self.path) as conn:
            conn.executescript('''
CREATE TABLE epub_jobs (id VARCHAR(32) PRIMARY KEY, trace_id VARCHAR(64), source_filename TEXT,
 input_path TEXT, output_path TEXT, output_mode VARCHAR(16), enable_translation BOOLEAN,
 target_lang VARCHAR(16), bilingual BOOLEAN, device VARCHAR(16), status VARCHAR(16),
 message TEXT, error_code VARCHAR(64), created_at DATETIME, updated_at DATETIME);
INSERT INTO epub_jobs VALUES ('legacy', 'trace', 'original.epub', '/private/source.epub',
 '/private/result.epub', 'simplified', 0, 'zh-CN', 0, 'generic', 'success', 'old-message',
 NULL, '2026-10-01 00:00:00', '2026-10-01 00:00:00');
CREATE TABLE job_chunks (id VARCHAR(160) PRIMARY KEY, job_id VARCHAR(32), source_hash VARCHAR(128));
INSERT INTO job_chunks VALUES ('chunk', 'legacy', 'source-checksum');
CREATE TABLE notifications (id VARCHAR(96) PRIMARY KEY, job_id VARCHAR(32), user_id VARCHAR(64),
 channel VARCHAR(32), status VARCHAR(32), payload_json TEXT, sent_at DATETIME,
 error_message TEXT, created_at DATETIME);
INSERT INTO notifications VALUES ('notice', 'legacy', NULL, 'in_app', 'sent',
 '{"status":"success"}', NULL, NULL, '2026-10-01 00:00:00');
''')

    def assert_legacy_preserved(self):
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT status,message,input_path,output_path FROM epub_jobs").fetchall(),
                             [('success', 'old-message', '/private/source.epub', '/private/result.epub')])
            self.assertEqual(conn.execute("SELECT id,source_hash FROM job_chunks").fetchall(), [('chunk', 'source-checksum')])
            self.assertEqual(conn.execute("SELECT id,payload_json FROM notifications").fetchall(), [('notice', '{"status":"success"}')])
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], 'ok')

    def test_real_multiprocess_first_start_on_empty_database(self):
        rows = self.race(["startup"] * 4)
        for row in rows:
            self.assertEqual(set(row['tables']), set(db.Base.metadata.tables))
            self.assertEqual(set(row['job_columns']), set(db.JobRecord.__table__.columns.keys()))
            self.assertIn('ix_notifications_job_channel_created_id', row['indexes'])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_real_multiprocess_startup_upgrade_preserves_legacy_rows(self):
        self.make_legacy()
        rows = self.race(["startup"] * 4)
        for row in rows:
            self.assertIn('job_dispatch_outbox', row['tables'])
            self.assertIn('job_executions', row['tables'])
            self.assertIn('payment_resolution_json', row['job_columns'])
            self.assertIn('audit_json', row['chunk_columns'])
        self.assert_legacy_preserved()

    def test_direct_ensure_and_startup_share_one_cross_process_lock(self):
        self.make_legacy()
        rows = self.race(["direct", "startup", "direct", "startup"])
        for row in rows:
            self.assertIn('payment_entitlement_json', row['job_columns'])
            self.assertIn('ix_notifications_channel_created_id', row['indexes'])
        self.assert_legacy_preserved()

    def test_direct_migration_callers_are_serialized(self):
        self.make_legacy()
        self.race(["direct"] * 4)
        self.assert_legacy_preserved()

    def test_stable_private_lock_survives_repeated_init(self):
        engine = self.initialize()
        lock = db._sqlite_schema_lock_path(engine)
        inode = lock.stat().st_ino
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.initialize()
        db._ensure_compatible_schema(engine)
        self.assertEqual(lock.stat().st_ino, inode)

    def test_lock_acquired_before_first_wal_connection(self):
        engine = self.engine()
        lock = db._sqlite_schema_lock_path(engine)
        with patch.object(db, 'create_engine', return_value=engine):
            @event.listens_for(engine, 'connect')
            def assert_locked(connection, record):
                fd = os.open(lock, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            self.initialize()

    def test_owner_process_death_releases_lock_without_unlink(self):
        context = multiprocessing.get_context('spawn')
        ready = context.Event()
        process = context.Process(target=_hold_schema, args=(self.url, ready))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            engine = self.engine()
            lock = db._sqlite_schema_lock_path(engine)
            inode = lock.stat().st_ino
            with self.assertRaises(TimeoutError):
                with db._sqlite_schema_lock(engine, timeout=0.05): pass
            self.assertFalse(self.path.exists())
            process.terminate(); process.join(timeout=5)
            self.initialize()
            self.assertEqual(lock.stat().st_ino, inode)
        finally:
            if process.is_alive(): process.terminate(); process.join(timeout=5)

    def test_symlink_lock_is_rejected_before_database_open(self):
        engine = self.engine()
        target = self.root / 'unrelated'
        target.write_bytes(b'keep')
        db._sqlite_schema_lock_path(engine).symlink_to(target)
        with self.assertRaises(OSError): self.initialize()
        self.assertEqual(target.read_bytes(), b'keep')
        self.assertFalse(self.path.exists())

    def test_nonregular_fifo_lock_is_rejected_without_hanging(self):
        engine = self.engine()
        os.mkfifo(db._sqlite_schema_lock_path(engine))
        with self.assertRaises(ValueError): self.initialize()
        self.assertFalse(self.path.exists())

    def test_real_schema_error_propagates_and_releases_lock(self):
        self.make_legacy()
        with sqlite3.connect(self.path) as conn:
            conn.execute('ALTER TABLE notifications RENAME COLUMN channel TO broken_channel')
        with self.assertRaises(OperationalError): self.initialize()
        lock = db._sqlite_schema_lock_path(self.engine())
        fd = os.open(lock, os.O_RDWR)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(fd)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT message FROM epub_jobs').fetchone()[0], 'old-message')

    def test_direct_real_migration_error_is_not_swallowed(self):
        engine = self.engine()
        with self.assertRaises(Exception) as error: db._ensure_compatible_schema(engine)
        self.assertEqual(type(error.exception).__name__, 'NoSuchTableError')

    def test_cleanup_failure_does_not_mask_original_schema_error(self):
        engine = self.engine()
        original = OperationalError('CREATE TABLE', {}, RuntimeError('schema rejected'))
        with patch.object(db, 'create_engine', return_value=engine), \
             patch.object(db.Base.metadata, 'create_all', side_effect=original), \
             patch.object(engine, 'dispose', side_effect=RuntimeError('cleanup rejected')):
            with self.assertRaises(OperationalError) as failure:
                db._make_engine()
        self.assertIs(failure.exception, original)

    def test_remote_uri_is_rejected_instead_of_locking_wrong_local_path(self):
        engine = self.engine('sqlite:///file://other-host/tmp/jobs.db?uri=true')
        with self.assertRaises(ValueError): db._sqlite_schema_lock_path(engine)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_uri_encoded_filename_and_plain_filename_share_lock_identity(self):
        path = self.root / 'space name.db'
        from urllib.parse import quote
        uri = 'sqlite:///file:' + quote(str(path)) + '?mode=rwc&uri=true'
        normal = self.engine('sqlite:///' + str(path))
        encoded = self.engine(uri)
        self.assertEqual(db._sqlite_schema_lock_path(normal), db._sqlite_schema_lock_path(encoded))
        with patch.dict(os.environ, {'DATABASE_URL': uri}):
            self.initialize()
        self.assertTrue(path.is_file())
        self.assertFalse((self.root / 'space%20name.db').exists())

    def test_relative_symlink_alias_uses_canonical_database_lock(self):
        self.path.touch()
        alias = self.root / 'alias.db'
        alias.symlink_to(self.path)
        self.assertEqual(db._sqlite_schema_lock_path(self.engine()),
                         db._sqlite_schema_lock_path(self.engine('sqlite:///' + str(alias))))

    def test_memory_urls_do_not_create_filesystem_locks(self):
        for url in ('sqlite://', 'sqlite:///:memory:',
                    'sqlite:///file::memory:?cache=shared&uri=true',
                    'sqlite:///file:private-test?mode=memory&cache=shared&uri=true'):
            with self.subTest(url=url), patch.dict(os.environ, {'DATABASE_URL': url}):
                engine = self.initialize()
                self.assertIsNone(db._sqlite_schema_lock_path(engine))
                self.assertIn('job_dispatch_outbox', inspect(engine).get_table_names())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_non_sqlite_dialect_has_no_local_lock_behavior(self):
        from types import SimpleNamespace
        engine = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
        with patch.object(db.os, 'open', side_effect=AssertionError('no filesystem access')):
            with db._sqlite_schema_lock(engine): pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
