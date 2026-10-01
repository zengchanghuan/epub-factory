"""Real OS fork and SQLite WAL proof; no broker, network or manuscript input."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import event, text

with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv", return_value=False):
    from app.storage_db import _make_engine, PersistentJobStore
    from app import storage
    from app.infra import worker_db_lifecycle as lifecycle


class WorkerDatabaseLifecycleTests(unittest.TestCase):
    def test_registration_is_idempotent_and_disposes_each_existing_engine_once(self):
        engine, extra = Mock(), Mock()
        ledger = Mock(_default_engine=engine, _ledgers={engine: object(), extra: object()})
        with patch.object(storage, "job_store", Mock(_engine=engine)), patch.dict(sys.modules, {"app.infra.llm_usage_ledger": ledger}):
            lifecycle.register_worker_db_lifecycle()
            lifecycle.register_worker_db_lifecycle()
            lifecycle.worker_before_create_process.send(sender=self)
            engine.dispose.assert_called_once_with(close=True)
            extra.dispose.assert_called_once_with(close=True)
            lifecycle.worker_process_init.send(sender=self)
            self.assertEqual(engine.dispose.call_args.kwargs, {"close": False})
            self.assertEqual(engine.dispose.call_count, 2)

    def test_memory_store_does_not_create_a_database(self):
        with patch.object(storage, "job_store", storage.JobStore()), patch.dict(sys.modules, {"app.infra.llm_usage_ledger": None}):
            self.assertEqual(list(lifecycle._known_engines()), [])
            lifecycle.before_worker_fork()
            lifecycle.after_worker_fork()

    @unittest.skipUnless(hasattr(os, "fork"), "OS fork is unavailable on this host")
    def test_real_prefork_replacement_children_and_parent_keep_independent_wal_connections(self):
        with tempfile.TemporaryDirectory(prefix="epub-r7-db-fork-") as temporary, \
                patch.dict(os.environ, {"DATABASE_URL": "sqlite:///" + str(Path(temporary) / "jobs.db")}, clear=True), \
                patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            engine = _make_engine()
            self.addCleanup(engine.dispose)
            @event.listens_for(engine, "connect")
            def record_pid(_connection, record):
                record.info["created_pid"] = os.getpid()
            with engine.connect() as connection:
                connection.info["created_pid"] = os.getpid()
                self.assertEqual(connection.execute(text("PRAGMA journal_mode")).scalar(), "wal")
            store = PersistentJobStore(engine)
            with patch.object(storage, "job_store", store), patch.dict(sys.modules, {"app.infra.llm_usage_ledger": None}):
                for _ in range(3):
                    reader, writer = os.pipe()
                    lifecycle.before_worker_fork()
                    child = os.fork()
                    if child == 0:
                        os.close(reader)
                        try:
                            lifecycle.after_worker_fork()
                            with engine.connect() as connection:
                                payload = {"count": connection.execute(text("SELECT count(*) FROM epub_jobs")).scalar(),
                                           "connection_pid": connection.info["created_pid"], "pid": os.getpid()}
                            os.write(writer, json.dumps(payload).encode())
                            os._exit(0)
                        except BaseException as exc:
                            os.write(writer, json.dumps({"error": type(exc).__name__, "message": str(exc)}).encode())
                            os._exit(1)
                    os.close(writer)
                    try:
                        import select
                        self.assertTrue(select.select([reader], [], [], 10)[0], "fork child stalled")
                        result = json.loads(os.read(reader, 8192).decode())
                        _, status = os.waitpid(child, 0)
                        self.assertEqual(status, 0, result)
                        self.assertEqual(result["pid"], result["connection_pid"])
                        self.assertEqual(result["count"], 0)
                        self.assertNotEqual(result["pid"], os.getpid())
                    finally:
                        os.close(reader)
                        try:
                            os.kill(child, 9)
                            os.waitpid(child, 0)
                        except ProcessLookupError:
                            pass
                        except ChildProcessError:
                            pass
                    with engine.connect() as connection:
                        self.assertEqual(connection.info["created_pid"], os.getpid())
                        self.assertEqual(connection.execute(text("SELECT count(*) FROM epub_jobs")).scalar(), 0)


if __name__ == "__main__":
    unittest.main()
