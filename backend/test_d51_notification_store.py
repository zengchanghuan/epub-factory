"""R13 scoped notification keyset pages, temporary SQLite + memory parity."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, inspect, text

# Importing storage must never select the developer's configured database.
with patch.dict(os.environ, {}, clear=True):
    from app.storage import JobStore
from app.models import Job, JobNotification, JobStatus, NotificationStatus, OutputMode
from app.storage_db import Base, JobRecord, NotificationRecord, PersistentJobStore, _ensure_compatible_schema

AT = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)


class NotificationStoreTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for target in ("connect", "connect_ex"):
            guard = self.stack.enter_context(patch.object(socket.socket, target, side_effect=AssertionError("network forbidden")))
            self.addCleanup(guard.assert_not_called)
        dns = self.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")))
        self.addCleanup(dns.assert_not_called)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.engine = create_engine("sqlite:///" + str(self.root / "jobs.db"), connect_args={"check_same_thread": False})
        self.stack.callback(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.memory, self.sql = JobStore(), PersistentJobStore(self.engine)
        self.stores = (self.memory, self.sql)

    @staticmethod
    def job(key="book", user="owner-a"):
        return Job(id=key, source_filename="never-read.epub", output_mode=OutputMode.simplified,
                   trace_id="offline", input_path="/never-opened/unused.epub", user_id=user,
                   status=JobStatus.pending_payment)

    @staticmethod
    def notification(job="book", **values):
        return JobNotification(job_id=job, channel=values.pop("channel", "in_app"), created_at=values.pop("created_at", AT),
                               status=NotificationStatus.sent, payload={"message": "done", "nested": {"a": 1}}, **values)

    def seed(self, store, *, count=8, job="book", user="owner-a"):
        store.add(self.job(job, user))
        for index in range(count):
            store.add_notification(self.notification(job, id=f"{index:032x}"))

    def test_default_identity_stable_and_old_positional_signature_preserved(self):
        notification = JobNotification("book", "in_app", NotificationStatus.sent, {}, None, None, None, AT)
        self.assertRegex(notification.id, r"^[0-9a-f]{32}$")
        for store in self.stores:
            saved = store.add_notification(notification)
            self.assertEqual(saved.id, notification.id)
            self.assertEqual(store.list_notifications("book")[0].id, notification.id)
            self.assertEqual(store.list_notification_page(job_id="book")[0].id, notification.id)

    def test_concurrent_same_timestamp_notifications_have_distinct_ids(self):
        for store in self.stores:
            store.add(self.job())
            def add(index): return store.add_notification(self.notification())
            with ThreadPoolExecutor(max_workers=8) as pool: rows = list(pool.map(add, range(64)))
            self.assertEqual(len({row.id for row in rows}), 64)
            page = store.list_notification_page(job_id="book", limit=101)
            self.assertEqual([row.id for row in page], sorted((row.id for row in rows), reverse=True))
            self.assertEqual({row.created_at for row in page}, {AT})

    def test_duplicate_id_is_rejected_without_mutation_or_invisible_extra_row(self):
        from sqlalchemy.exc import IntegrityError
        for store in self.stores:
            original = self.notification(id="same-stable-id")
            saved = store.add_notification(original)
            duplicate = self.notification(id=original.id, created_at=AT + timedelta(seconds=1))
            duplicate.payload = {"message": "must not overwrite original"}
            duplicate.job_id = "different-job"
            with self.assertRaises((ValueError, IntegrityError)):
                store.add_notification(duplicate)
            self.assertEqual(len(store.list_notifications()), 1)
            page = store.list_notification_page(job_id="book", limit=1)
            self.assertEqual(page, [saved])
            self.assertEqual(store.list_notification_page(job_id="different-job"), [])
            self.assertEqual(store.list_notification_page(job_id="book", limit=1,
                before=(page[0].created_at, page[0].id)), [])

    def test_job_and_user_scopes_are_intersection_and_user_follows_parent(self):
        for store in self.stores:
            store.add(self.job("a1", "owner-a")); store.add(self.job("a2", "owner-a"))
            store.add(self.job("b", "owner-b")); store.add(self.job("anonymous", None))
            for job, misleading_user in (("a1", "owner-b"), ("a2", None), ("b", "owner-a"),
                                         ("anonymous", "owner-a"), ("orphan", "owner-a")):
                store.add_notification(self.notification(job, user_id=misleading_user))
            self.assertEqual({n.job_id for n in store.list_notification_page(user_id="owner-a")}, {"a1", "a2"})
            self.assertEqual({n.job_id for n in store.list_notification_page(job_id="a1", user_id="owner-a")}, {"a1"})
            self.assertEqual(store.list_notification_page(job_id="b", user_id="owner-a"), [])
            self.assertEqual(store.list_notification_page(user_id="missing"), [])

    def test_changed_parent_ownership_immediately_changes_user_visibility(self):
        for store in self.stores:
            self.seed(store, count=1)
            if store is self.memory:
                store.add(self.job(user="owner-b"))
            else:
                with self.sql._Session.begin() as session:
                    session.query(JobRecord).filter_by(id="book").update({"user_id": "owner-b"})
            self.assertEqual(store.list_notification_page(user_id="owner-a"), [])
            self.assertEqual(len(store.list_notification_page(user_id="owner-b")), 1)

    def test_public_pages_never_return_private_email_channels(self):
        for store in self.stores:
            store.add(self.job())
            for channel in ("in_app", "email", "payment_email", "unknown"):
                store.add_notification(self.notification(channel=channel))
            self.assertEqual(len(store.list_notification_page(user_id="owner-a")), 1)
            self.assertEqual(len(store.list_notification_page(job_id="book")), 1)
            self.assertEqual(len(store.list_notifications("book")), 4)

    def test_equal_timestamp_keyset_has_no_duplicates_or_gaps(self):
        for store in self.stores:
            self.seed(store)
            rows, before = [], None
            while True:
                page = store.list_notification_page(user_id="owner-a", limit=3, before=before)
                if not page: break
                rows.extend(page)
                before = (page[-1].created_at, page[-1].id)
            self.assertEqual([n.id for n in rows], [f"{i:032x}" for i in reversed(range(8))])
            self.assertTrue(all(n.created_at.utcoffset() == timedelta(0) for n in rows))

    def test_new_notifications_do_not_shift_existing_cursor_pages(self):
        for store in self.stores:
            self.seed(store)
            first = store.list_notification_page(job_id="book", limit=3)
            store.add_notification(self.notification(id="f" * 32, created_at=AT + timedelta(seconds=1)))
            tail = store.list_notification_page(job_id="book", limit=101, before=(first[-1].created_at, first[-1].id))
            self.assertEqual([n.id for n in first + tail], [f"{i:032x}" for i in reversed(range(8))])
            self.assertEqual(store.list_notification_page(job_id="book", limit=1)[0].id, "f" * 32)

    def test_timestamp_precedes_id_and_cursor_is_strict(self):
        for store in self.stores:
            store.add(self.job())
            for identity, when in (("z", AT), ("a", AT + timedelta(seconds=1)), ("m", AT)):
                store.add_notification(self.notification(id=identity, created_at=when))
            self.assertEqual([n.id for n in store.list_notification_page(job_id="book")], ["a", "z", "m"])
            self.assertEqual([n.id for n in store.list_notification_page(job_id="book", before=(AT, "z"))], ["m"])

    def test_limit_default_minimum_and_maximum_are_enforced_in_both_stores(self):
        for store in self.stores:
            self.seed(store, count=105)
            self.assertEqual(len(store.list_notification_page(job_id="book")), 21)
            self.assertEqual(len(store.list_notification_page(job_id="book", limit=1)), 1)
            self.assertEqual(len(store.list_notification_page(job_id="book", limit=101)), 101)

    def test_invalid_scope_limit_and_cursor_rejected_before_sql(self):
        statements = []
        def observed(conn, cursor, statement, parameters, context, executemany): statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", observed)
        self.addCleanup(lambda: event.remove(self.engine, "before_cursor_execute", observed))
        cases = [{}, {"job_id": ""}, {"user_id": " "}, {"job_id": False}, {"user_id": 3}]
        cases += [{"job_id": "book", "limit": value} for value in (0, 102, -1, True, False, 1.0, "21", None)]
        cases += [{"job_id": "book", "before": value} for value in (
            [], (AT,), (AT, "a", "b"), [AT, "a"], (AT.replace(tzinfo=None), "a"),
            (AT.astimezone(timezone(timedelta(hours=8))), "a"), ("2026-10-02", "a"),
            (AT, ""), (AT, " " ), (AT, False), (AT, "x"*97))]
        for store in self.stores:
            for values in cases:
                with self.subTest(values=values, store=type(store).__name__), self.assertRaises(ValueError):
                    store.list_notification_page(**values)
        self.assertEqual(statements, [])

    def test_sql_uses_ordered_limit_and_current_parent_join_not_full_list(self):
        self.seed(self.sql)
        statements = []
        def observed(conn, cursor, statement, parameters, context, executemany): statements.append((statement, parameters))
        event.listen(self.engine, "before_cursor_execute", observed)
        self.addCleanup(lambda: event.remove(self.engine, "before_cursor_execute", observed))
        with patch.object(self.sql, "list_notifications", side_effect=AssertionError("unbounded list forbidden")):
            self.sql.list_notification_page(user_id="owner-a", limit=3, before=(AT, "f"*32))
        self.assertEqual(len(statements), 1)
        statement, parameters = statements[0]
        self.assertIn("JOIN epub_jobs", statement)
        self.assertIn("epub_jobs.user_id", statement)
        self.assertNotIn("notifications.user_id =", statement)
        self.assertIn("ORDER BY notifications.created_at DESC, notifications.id DESC", statement)
        self.assertIn("LIMIT", statement)
        self.assertEqual(parameters[-2:], (3, 0))

    def test_old_database_ids_survive_reload_and_index_migration_is_additive(self):
        self.sql.add(self.job())
        legacy_id = "book:in_app:1722470400000"
        with self.sql._Session.begin() as session:
            session.add(NotificationRecord(id=legacy_id, job_id="book", user_id="incorrect",
                channel="in_app", status="sent", payload_json='{"message":"legacy"}', created_at=AT))
        indexes = ("ix_notifications_job_channel_created_id", "ix_notifications_channel_created_id", "ix_epub_jobs_user_id_id")
        with self.engine.begin() as conn:
            for index in indexes: conn.execute(text(f"DROP INDEX {index}"))
        for _ in range(2): _ensure_compatible_schema(self.engine)
        existing = {row["name"] for table in ("notifications", "epub_jobs") for row in inspect(self.engine).get_indexes(table)}
        self.assertTrue(set(indexes) <= existing)
        reload = PersistentJobStore(self.engine)
        row = reload.list_notification_page(user_id="owner-a")[0]
        self.assertEqual(row.id, legacy_id)
        self.assertEqual(row.payload, {"message": "legacy"})
        self.assertEqual(row.created_at, AT)
        self.assertEqual(reload.list_notifications("book")[0].id, legacy_id)

    def test_notification_snapshots_cannot_mutate_saved_id_or_payload(self):
        for store in self.stores:
            notification = self.notification()
            original = notification.id
            saved = store.add_notification(notification)
            notification.payload["nested"]["a"] = 2
            saved.id = "changed"; saved.payload["nested"]["a"] = 3
            row = store.list_notification_page(job_id="book")[0]
            self.assertEqual(row.id, original); self.assertEqual(row.payload["nested"]["a"], 1)
            row.payload["nested"]["a"] = 4
            self.assertEqual(store.list_notification_page(job_id="book")[0].payload["nested"]["a"], 1)

    def test_non_utc_new_timestamp_normalized_and_legacy_naive_is_utc(self):
        for store in self.stores:
            for when in (AT.astimezone(timezone(timedelta(hours=8))), AT.replace(tzinfo=None)):
                saved = store.add_notification(self.notification(created_at=when))
                self.assertEqual(saved.created_at, AT)
                self.assertEqual(saved.created_at.utcoffset(), timedelta(0))
            self.assertTrue(all(n.created_at == AT for n in store.list_notification_page(job_id="book")))


if __name__ == "__main__": unittest.main()
