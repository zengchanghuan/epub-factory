"""R8 atomic parent fencing; real SQLite, memory parity, no network/model calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import event

import test_d45_execution_store as fixtures
from app.models import JobChapter, JobChunk, JobStage, JobStatus
from app.domain.job_write_fence import JobWriteConflict, current_job_write_fence, job_write_scope
from app.storage_db import ChunkRecord, PersistentJobStore


class WriteFenceStoreTests(unittest.TestCase):
    setUp = fixtures.ExecutionStoreTests.setUp
    job = fixtures.ExecutionStoreTests.job

    def start(self, store, key="book", attempt="attempt"):
        store.add(self.job(key, translation_stats={"attempt_id": attempt, "nested": {"x": 1}}))
        self.assertTrue(store.begin_execution(key, attempt, "owner", now=100))

    def chapter(self, key="book"):
        return JobChapter(key, "chapter", "text.xhtml")

    def chunk(self, key="book"):
        return JobChunk(key, "chapter", "chunk", 1, "p:1", "source", audit_json={"nested": {"x": 1}})

    def writes(self, store, key="book", **kwargs):
        return [lambda: store.update_status(key, JobStatus.running, "late", **kwargs),
                lambda: store.upsert_chapter(self.chapter(key), **kwargs),
                lambda: store.upsert_chunk(self.chunk(key), **kwargs),
                lambda: store.add_stage(JobStage(key, "late"), **kwargs),
                lambda: store.clear_translation_progress(key, **kwargs)]

    def test_scoped_owner_writes_all_records_and_may_commit_terminal_once(self):
        for store in self.stores:
            self.start(store)
            with job_write_scope("book", "attempt", "owner"):
                for write in self.writes(store):
                    self.assertIsNotNone(write())
                saved = store.update_status("book", JobStatus.success, "finished", output_path="/output.epub")
                self.assertEqual(saved.status, JobStatus.success)
                with self.assertRaises(JobWriteConflict):
                    store.update_status("book", JobStatus.running, "late")

    def test_cancelled_job_rejects_every_old_write_and_bypass_flag(self):
        for store in self.stores:
            self.start(store)
            store.upsert_chunk(self.chunk())
            store.update_status("book", JobStatus.cancelled, "user cancelled")
            before = store.get("book")
            with job_write_scope("book", "attempt", "owner"):
                for write in self.writes(store):
                    with self.assertRaises(JobWriteConflict):
                        write()
                with self.assertRaises(JobWriteConflict):
                    store.update_status("book", JobStatus.success, allow_cancelled_transition=True)
            self.assertEqual(store.get("book"), before)
            self.assertEqual(len(store.list_chunks("book")), 1)
            self.assertEqual(store.list_stages("book"), [])

    def test_same_attempt_recovered_new_owner_fences_old_owner(self):
        for store in self.stores:
            self.start(store)
            self.assertEqual(store.recover_execution("book", "attempt", "owner", stale_before=200, now=300), "recovered")
            self.assertTrue(store.begin_execution("book", "attempt", "new-owner", now=400))
            with job_write_scope("book", "attempt", "owner"):
                for write in self.writes(store):
                    with self.assertRaises(JobWriteConflict):
                        write()
            with job_write_scope("book", "attempt", "new-owner"):
                self.assertIsNotNone(store.upsert_chunk(self.chunk()))
            self.assertEqual(store.get_execution("book", "attempt")["recoveries"], 1)

    def test_new_attempt_rejects_old_writer_and_preserves_new_progress(self):
        for store in self.stores:
            self.start(store)
            store.update_status("book", JobStatus.failed)
            fresh, reason = store.restart_translation_attempt("book", attempt_id="next", action_label="retry",
                max_free_retries=-1, started_at=datetime.now(timezone.utc))
            self.assertEqual(reason, "ok")
            self.assertTrue(store.begin_execution("book", "next", "new-owner"))
            with job_write_scope("book", "next", "new-owner"):
                store.upsert_chunk(self.chunk())
            with job_write_scope("book", "attempt", "owner"):
                for write in self.writes(store):
                    with self.assertRaises(JobWriteConflict):
                        write()
            self.assertEqual(len(store.list_chunks("book")), 1)
            self.assertEqual(store.get("book").translation_stats["attempt_id"], "next")

    def test_empty_attempt_is_a_real_fence_not_a_wildcard(self):
        for store in self.stores:
            self.start(store, attempt="")
            with job_write_scope("book", "", "owner"):
                self.assertIsNotNone(store.upsert_chunk(self.chunk(), expected_attempt_id=""))
            store.update_status("book", JobStatus.running, translation_stats={"attempt_id": "new"})
            before = store.get("book")
            results = [write() for write in self.writes(store, expected_attempt_id="")]
            self.assertEqual(results, [before, None, None, None, False])
            self.assertEqual(store.get("book"), before)

    def test_scope_cannot_write_another_job_or_replace_its_attempt(self):
        for store in self.stores:
            self.start(store)
            self.start(store, "other")
            with job_write_scope("book", "attempt", "owner"):
                for write in self.writes(store, "other"):
                    with self.assertRaises(JobWriteConflict):
                        write()
                with self.assertRaises(JobWriteConflict):
                    store.update_status("book", JobStatus.running, translation_stats={"attempt_id": "new"})
                with self.assertRaises(JobWriteConflict):
                    store.upsert_chunk(self.chunk(), expected_attempt_id="")

    def test_command_status_and_timestamp_cas_returns_current_without_mutation(self):
        for store in self.stores:
            self.start(store)
            snapshot = store.get("book")
            changed = store.update_status("book", JobStatus.running, "new progress")
            rejected = store.update_status("book", JobStatus.cancelled, expected_updated_at=snapshot.updated_at,
                                           expected_statuses={JobStatus.running})
            self.assertEqual(rejected, changed)
            self.assertEqual(store.get("book"), changed)
            self.assertIsNone(store.add_stage(JobStage("book", "stale"), expected_updated_at=snapshot.updated_at))
            self.assertFalse(store.clear_translation_progress("book", expected_statuses={JobStatus.pending}))
            accepted = store.update_status("book", JobStatus.cancelled, expected_updated_at=changed.updated_at.replace(tzinfo=None),
                                           expected_statuses={"running"}, expected_attempt_id="attempt")
            self.assertEqual(accepted.status, JobStatus.cancelled)

    def test_missing_job_rejected_and_scoped_context_is_reset(self):
        for store in self.stores:
            self.assertEqual([write() for write in self.writes(store)], [None, None, None, None, False])
            with self.assertRaises(JobWriteConflict), job_write_scope("book", "", "owner"):
                store.add_stage(JobStage("book", "missing"))
            self.assertIsNone(current_job_write_fence())

    def test_scopes_propagate_into_async_tasks_and_reset_after_nested_scope(self):
        async def capture():
            await asyncio.sleep(0)
            return current_job_write_fence()
        with job_write_scope("book", "", "owner") as outer:
            self.assertEqual(asyncio.run(capture()), outer)
            with job_write_scope("other", "next", "owner-2"):
                self.assertEqual(current_job_write_fence().job_id, "other")
            self.assertEqual(current_job_write_fence(), outer)
        self.assertIsNone(current_job_write_fence())

    def test_input_read_and_return_objects_are_snapshots_in_both_stores(self):
        for store in self.stores:
            job = self.job(translation_stats={"attempt_id": "attempt", "nested": {"x": 1}})
            store.add(job)
            job.translation_stats["nested"]["x"] = 2
            read = store.get("book")
            read.translation_stats["nested"]["x"] = 3
            store.list_jobs()[0].status = JobStatus.failed
            self.assertEqual(store.get("book").translation_stats["nested"]["x"], 1)
            self.assertEqual(store.get("book").status, JobStatus.pending)
            for record, save, listing in ((self.chunk(), store.upsert_chunk, store.list_chunks),
                    (self.chapter(), store.upsert_chapter, store.list_chapters),
                    (JobStage("book", "one", metadata={"nested": {"x": 1}}), store.add_stage, store.list_stages)):
                result = save(record)
                result.job_id = "other"
                record.job_id = "other"
                listed = listing("book")
                listed[0].job_id = "other"
                self.assertEqual(listing("book")[0].job_id, "book")
            chunk = store.list_chunks("book")[0]
            chunk.audit_json["nested"]["x"] = 4
            self.assertEqual(store.list_chunks("book")[0].audit_json["nested"]["x"], 1)
            stats = {"nested": {"x": 5}}
            saved = store.update_status("book", JobStatus.running, translation_stats=stats)
            stats["nested"]["x"] = 6
            saved.translation_stats["nested"]["x"] = 7
            self.assertEqual(store.get("book").translation_stats["nested"]["x"], 5)

    def test_sql_child_write_holds_parent_lock_until_commit_then_cancel_wins(self):
        self.start(self.sql)
        entered, release = threading.Event(), threading.Event()
        def pause(_mapper, _connection, _target):
            entered.set()
            self.assertTrue(release.wait(5))
        def write():
            with job_write_scope("book", "attempt", "owner"):
                return self.sql.upsert_chunk(self.chunk())
        event.listen(ChunkRecord, "before_insert", pause)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                old = pool.submit(write)
                self.assertTrue(entered.wait(5))
                cancel = pool.submit(self.sql.update_status, "book", JobStatus.cancelled, "cancel",
                                     expected_statuses={JobStatus.running}, expected_attempt_id="attempt")
                try:
                    with self.assertRaises(TimeoutError):
                        cancel.result(timeout=0.05)
                finally:
                    release.set()
                self.assertIsNotNone(old.result(timeout=5))
                self.assertEqual(cancel.result(timeout=5).status, JobStatus.cancelled)
        finally:
            event.remove(ChunkRecord, "before_insert", pause)
            release.set()
        with job_write_scope("book", "attempt", "owner"), self.assertRaises(JobWriteConflict):
            self.sql.upsert_chunk(self.chunk())

    def test_sql_rejected_parent_cas_does_not_change_child_records(self):
        self.start(self.sql)
        self.sql.upsert_chunk(self.chunk())
        before = self.sql.get("book")
        with job_write_scope("book", "attempt", "owner"):
            with patch("app.storage_db.check_job_write", side_effect=JobWriteConflict("injected")):
                for write in self.writes(self.sql):
                    with self.assertRaises(JobWriteConflict):
                        write()
        reopened = PersistentJobStore(self.engine)
        self.assertEqual(reopened.get("book"), before)
        self.assertEqual(len(reopened.list_chunks("book")), 1)
        self.assertEqual(reopened.list_stages("book"), [])

    def test_final_status_update_has_owner_predicate_and_rowcount_zero_rolls_back(self):
        self.start(self.sql)
        before = self.sql.get("book")
        seen = []
        def alter_snapshot(connection, _cursor, statement, _parameters, _context, _executemany):
            # Mutate inside this same transaction immediately before final CAS,
            # after the policy check. This deterministically exercises rowcount=0.
            if statement.startswith("UPDATE epub_jobs SET ") and "message=" in statement and "EXISTS" in statement:
                seen.append(statement)
                connection.exec_driver_sql("UPDATE epub_jobs SET translation_stats_json='{}' WHERE id='book'")
        event.listen(self.engine, "before_cursor_execute", alter_snapshot)
        try:
            with job_write_scope("book", "attempt", "owner"), self.assertRaises(JobWriteConflict):
                self.sql.update_status("book", JobStatus.success, "must not commit", output_path="/stale.epub")
        finally:
            event.remove(self.engine, "before_cursor_execute", alter_snapshot)
        self.assertEqual(len(seen), 1)
        where = seen[0].split(" WHERE ", 1)[1]
        for predicate in ("epub_jobs.status", "epub_jobs.updated_at", "epub_jobs.translation_stats_json", "EXISTS", "job_executions.owner"):
            self.assertIn(predicate, where)
        self.assertEqual(self.sql.get("book"), before)

    def test_failed_child_flush_rolls_back_parent_and_preserves_existing_rows(self):
        self.start(self.sql)
        before = self.sql.get("book")
        def fail(*_args):
            raise RuntimeError("injected child flush failure")
        event.listen(ChunkRecord, "before_insert", fail)
        try:
            with job_write_scope("book", "attempt", "owner"), self.assertRaisesRegex(RuntimeError, "injected"):
                self.sql.upsert_chunk(self.chunk())
        finally:
            event.remove(ChunkRecord, "before_insert", fail)
        self.assertEqual(self.sql.get("book"), before)
        self.assertEqual(self.sql.list_chunks("book"), [])

    def test_restart_erases_old_child_committed_before_it_and_late_old_write_is_rejected(self):
        self.start(self.sql)
        self.sql.update_status("book", JobStatus.failed)
        entered, release = threading.Event(), threading.Event()
        def pause(_mapper, _connection, _target):
            entered.set()
            self.assertTrue(release.wait(5))
        event.listen(ChunkRecord, "before_insert", pause)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                child = pool.submit(self.sql.upsert_chunk, self.chunk(), expected_attempt_id="attempt",
                                    expected_statuses={JobStatus.failed})
                self.assertTrue(entered.wait(5))
                retry = pool.submit(self.sql.restart_translation_attempt, "book", attempt_id="next", action_label="retry",
                                    max_free_retries=-1, started_at=datetime.now(timezone.utc))
                try:
                    with self.assertRaises(TimeoutError):
                        retry.result(timeout=0.05)
                finally:
                    release.set()
                self.assertIsNotNone(child.result(timeout=5))
                self.assertEqual(retry.result(timeout=5)[1], "ok")
        finally:
            event.remove(ChunkRecord, "before_insert", pause)
            release.set()
        self.assertEqual(self.sql.list_chunks("book"), [])
        self.assertIsNone(self.sql.upsert_chunk(self.chunk(), expected_attempt_id="attempt"))
        self.assertFalse(self.sql.clear_translation_progress("book", expected_attempt_id="attempt"))
        self.assertEqual(self.sql.get("book").translation_stats["attempt_id"], "next")

    def test_precision_cancel_uses_latest_counters_and_marks_review_not_refund(self):
        for store in self.stores:
            for test_order in (False, True):
                key = "test" if test_order else "paid"
                store.add(self.job(key, enable_precision_polish=True, is_test_order=test_order,
                    translation_stats={"attempt_id": "attempt", "cached_chunks": 3,
                        "precision_polish": {"status": "running", "quoted_amount": "6.00", "api_calls": 1,
                                             "reviewed": 2, "validation_passed": True, "custom": "keep"}}))
                self.assertTrue(store.begin_execution(key, "attempt", "owner"))
                stale = store.get(key).translation_stats
                with job_write_scope(key, "attempt", "owner"):
                    store.update_status(key, JobStatus.running, translation_stats={"cached_chunks": 9,
                        "precision_polish": {**stale["precision_polish"], "api_calls": 5, "reviewed": 8}})
                cancelled = store.update_status(key, JobStatus.cancelled, expected_attempt_id="attempt",
                    expected_statuses={JobStatus.running}, translation_stats=stale)
                precision = cancelled.translation_stats["precision_polish"]
                self.assertEqual((precision["status"], precision["reason"], precision["validation_passed"]),
                                 ("cancelled", "cancelled", False))
                self.assertEqual((precision["api_calls"], precision["reviewed"], precision["quoted_amount"], precision["custom"]),
                                 (5, 8, "6.00", "keep"))
                self.assertEqual(precision["refund_required"], not test_order)
                self.assertNotIn("refunded", precision)
                self.assertEqual(cancelled.translation_stats["cached_chunks"], 9)
                with job_write_scope(key, "attempt", "owner"), self.assertRaises(JobWriteConflict):
                    store.update_status(key, JobStatus.running, translation_stats=stale)
                self.assertEqual(store.get(key).translation_stats, cancelled.translation_stats)
                if store is self.sql:
                    self.assertEqual(PersistentJobStore(self.engine).get(key).translation_stats, cancelled.translation_stats)

    def test_sql_precision_cancel_waits_for_current_progress_then_merges_it(self):
        self.sql.add(self.job(enable_precision_polish=True, translation_stats={"attempt_id": "attempt",
            "precision_polish": {"quoted_amount": "6.00", "api_calls": 0}}))
        self.sql.begin_execution("book", "attempt", "owner")
        entered, release = threading.Event(), threading.Event()
        def pause(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.startswith("UPDATE epub_jobs SET ") and "message=" in statement and "EXISTS" in statement:
                entered.set()
                self.assertTrue(release.wait(5))
        def progress():
            with job_write_scope("book", "attempt", "owner"):
                return self.sql.update_status("book", JobStatus.running, translation_stats={
                    "precision_polish": {"quoted_amount": "6.00", "api_calls": 11, "reviewed": 23}})
        event.listen(self.engine, "before_cursor_execute", pause)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(progress)
                self.assertTrue(entered.wait(5))
                cancel = pool.submit(self.sql.update_status, "book", JobStatus.cancelled,
                                    expected_attempt_id="attempt", expected_statuses={JobStatus.running})
                try:
                    with self.assertRaises(TimeoutError):
                        cancel.result(timeout=0.05)
                finally:
                    release.set()
                writer.result(timeout=5)
                precision = cancel.result(timeout=5).translation_stats["precision_polish"]
                self.assertEqual((precision["status"], precision["api_calls"], precision["reviewed"]), ("cancelled", 11, 23))
                self.assertTrue(precision["refund_required"])
        finally:
            event.remove(self.engine, "before_cursor_execute", pause)
            release.set()


if __name__ == "__main__":
    unittest.main()
