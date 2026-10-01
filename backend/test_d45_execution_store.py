"""R7 execution metadata and atomic stale recovery; isolated memory/SQLite."""
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, text

with patch.dict(os.environ, {}, clear=True):
    from app.storage import JobStore
from app.models import Job, JobStatus, OutputMode
from app.storage_db import Base, ExecutionRecord, PersistentJobStore
from app.domain.dispatch_intent import dispatch_identity, timestamp


class ExecutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("socket.socket.connect", "socket.getaddrinfo"):
            self.stack.enter_context(patch(name, side_effect=AssertionError("Network forbidden")))
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="epub-r7-store-")))
        self.engine = create_engine("sqlite:///" + str(root / "jobs.db"), connect_args={"check_same_thread": False})
        self.stack.callback(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.memory, self.sql = JobStore(), PersistentJobStore(self.engine)
        self.stores = (self.memory, self.sql)

    def job(self, key="book", **values):
        fields = dict(id=key, source_filename="unused.epub", input_path="/never-opened", trace_id="offline",
                      output_mode=OutputMode.simplified, status=JobStatus.pending,
                      translation_stats={"attempt_id": "attempt", "cached_chunks": 19, "prompt_tokens": 77},
                      updated_at=datetime.fromtimestamp(100, timezone.utc))
        fields.update(values)
        return Job(**fields)

    def begin(self, store, key="book", *, owner="owner", now=100):
        return store.begin_execution(key, "attempt", owner, now=now)

    def recover(self, store, key="book", *, owner="owner", now=300, cutoff=200, cap=2):
        return store.recover_execution(key, "attempt", owner, stale_before=cutoff, now=now, max_recoveries=cap)

    def test_begin_is_pending_only_exact_attempt_and_preserves_stats(self):
        for store in self.stores:
            store.add(self.job())
            self.assertFalse(store.begin_execution("book", "other", "owner", now=100))
            self.assertTrue(self.begin(store))
            self.assertEqual(store.get("book").status, JobStatus.running)
            self.assertEqual(store.get("book").translation_stats["prompt_tokens"], 77)
            record = store.get_execution("book", "attempt")
            self.assertEqual((record["owner"], record["state"], record["heartbeat_at"], record["recoveries"]),
                             ("owner", "running", 100, 0))
            self.assertFalse(self.begin(store, owner="duplicate"))
            self.assertIsNone(store.get_execution("book", "other"))

    def test_unpaid_cancelled_and_terminal_cannot_begin_or_recover(self):
        for store in self.stores:
            for status in JobStatus:
                if status in {JobStatus.pending, JobStatus.running}:
                    continue
                store.add(self.job(status.value, status=status))
                self.assertFalse(self.begin(store, status.value))
                self.assertEqual(self.recover(store, status.value), "unchanged")
                self.assertEqual(store.get(status.value).status, status)
                self.assertIsNone(store.get_execution(status.value, "attempt"))

    def test_concurrent_begin_has_one_owner(self):
        for store in self.stores:
            store.add(self.job())
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda index: self.begin(store, owner=f"owner-{index}"), range(12)))
            self.assertEqual(sum(results), 1)
            winner = f"owner-{results.index(True)}"
            self.assertEqual(store.get_execution("book", "attempt")["owner"], winner)

    def test_heartbeat_fences_owner_attempt_and_only_running_job(self):
        for store in self.stores:
            store.add(self.job())
            self.begin(store)
            self.assertFalse(store.heartbeat_execution("book", "attempt", "other", now=180))
            self.assertFalse(store.heartbeat_execution("book", "wrong", "owner", now=180))
            self.assertTrue(store.heartbeat_execution("book", "attempt", "owner", now=180))
            self.assertEqual(store.get_execution("book", "attempt")["heartbeat_at"], 180)
            store.update_status("book", JobStatus.cancelled)
            self.assertFalse(store.heartbeat_execution("book", "attempt", "owner", now=190))

    def test_finish_requires_terminal_current_job_and_does_not_change_job(self):
        for store in self.stores:
            store.add(self.job())
            self.begin(store)
            self.assertFalse(store.finish_execution("book", "attempt", "owner", now=190))
            store.update_status("book", JobStatus.success, "complete", output_path="/preserve.epub")
            before = store.get("book").updated_at
            self.assertFalse(store.finish_execution("book", "attempt", "other", now=190))
            self.assertTrue(store.finish_execution("book", "attempt", "owner", now=190))
            self.assertEqual(store.get_execution("book", "attempt")["state"], "finished")
            self.assertEqual(store.get_execution("book", "attempt")["owner"], "")
            self.assertEqual(store.get("book").updated_at, before)
            self.assertEqual(store.get("book").output_path, "/preserve.epub")
            self.assertFalse(store.finish_execution("book", "attempt", "owner"))

    def test_confirmation_and_payment_wait_are_not_execution_terminal_states(self):
        for store in self.stores:
            store.add(self.job()); self.begin(store)
            for state in (JobStatus.pending, JobStatus.pending_payment, JobStatus.awaiting_confirmation, JobStatus.confirming):
                store.update_status("book", state)
                self.assertFalse(store.finish_execution("book", "attempt", "owner"))
            self.assertEqual(store.get_execution("book", "attempt")["state"], "running")

    def test_list_stale_uses_heartbeats_legacy_fallback_and_oldest_first(self):
        for store in self.stores:
            store.add(self.job("fresh")); self.begin(store, "fresh", now=201)
            store.add(self.job("middle")); self.begin(store, "middle", now=150)
            store.add(self.job("oldest")); self.begin(store, "oldest", now=50)
            store.add(self.job("legacy", status=JobStatus.running))
            store.add(self.job("pending"))
            rows = store.list_stale_executions(stale_before=datetime.fromtimestamp(200, timezone.utc), limit=2)
            self.assertEqual([row["job_id"] for row in rows], ["oldest", "legacy"])
            self.assertEqual((rows[1]["owner"], rows[1]["legacy"]), ("", True))
            self.assertEqual(rows[0]["legacy"], False)
            self.assertEqual(len(store.list_stale_executions(stale_before=200)), 3)

    def test_recovery_retains_attempt_counters_and_invalidates_publisher_lease(self):
        for store in self.stores:
            store.add(self.job())
            publishing = store.claim_dispatch(now=timestamp() + 1)
            self.begin(store)
            stats = dict(store.get("book").translation_stats)
            self.assertEqual(self.recover(store), "recovered")
            self.assertEqual(store.get("book").status, JobStatus.pending)
            self.assertEqual(store.get("book").translation_stats, stats)
            execution = store.get_execution("book", "attempt")
            self.assertEqual((execution["state"], execution["owner"], execution["recoveries"]), ("queued", "", 1))
            dispatch = store.get_dispatch(dispatch_identity("book", "attempt"))
            self.assertEqual((dispatch["status"], dispatch["lease_token"], dispatch["next_attempt_at"]), ("pending", "", 300))
            self.assertFalse(store.finish_dispatch(publishing["dispatch_id"], publishing["lease_token"]))
            self.assertFalse(store.heartbeat_execution("book", "attempt", "owner", now=301))
            self.assertEqual(self.recover(store), "unchanged")

    def test_recovery_cap_survives_new_executor_and_fails_explicitly(self):
        for store in self.stores:
            store.add(self.job(output_path="/stale-result.epub", translation_stats={"attempt_id": "attempt", "live": True, "deliverable": True, "cached_chunks": 19}))
            self.begin(store)
            self.assertEqual(self.recover(store), "recovered")
            self.assertTrue(self.begin(store, owner="second", now=310))
            self.assertEqual(self.recover(store, owner="second", cutoff=400, now=410), "recovered")
            self.assertTrue(self.begin(store, owner="third", now=420))
            self.assertEqual(store.get_execution("book", "attempt")["recoveries"], 2)
            self.assertEqual(self.recover(store, owner="third", cutoff=500, now=510), "exhausted")
            job = store.get("book")
            self.assertEqual((job.status, job.error_code, job.output_path), (JobStatus.failed, "WORKER_RECOVERY_EXHAUSTED", None))
            self.assertEqual(job.translation_stats, {"attempt_id": "attempt", "live": False, "deliverable": False, "cached_chunks": 19})
            self.assertEqual(store.get_execution("book", "attempt")["state"], "exhausted")
            self.assertEqual(store.get_dispatch(dispatch_identity("book", "attempt"))["status"], "obsolete")
            self.assertFalse(self.begin(store, owner="duplicate"))
            self.assertEqual(self.recover(store, owner="third", cutoff=999), "unchanged")

    def test_zero_cap_exhausts_without_requeue(self):
        for store in self.stores:
            store.add(self.job()); self.begin(store)
            self.assertEqual(self.recover(store, cap=0), "exhausted")
            self.assertEqual(store.get_execution("book", "attempt")["recoveries"], 0)

    def test_exhaustion_invalidates_translation_qa_without_losing_paid_work(self):
        for store in self.stores:
            store.add(self.job(enable_translation=True, translation_stats={
                "attempt_id": "attempt", "live": True, "deliverable": True,
                "cached_chunks": 19, "prompt_tokens": 77, "free_retry_count": 2,
                "qa_report": {"status": "passed", "can_deliver": True},
            }))
            self.begin(store)
            with patch.dict(os.environ, {"EPUB_TRANSLATION_MAX_FREE_RETRIES": "2"}):
                self.assertEqual(self.recover(store, cap=0), "exhausted")
            job = store.get("book")
            stats, qa = job.translation_stats, job.translation_stats["qa_report"]
            self.assertEqual((stats["cached_chunks"], stats["prompt_tokens"]), (19, 77))
            self.assertEqual((qa["status"], qa["delivery_status"], qa["can_deliver"], qa["retryable"]),
                             ("failed", "failed", False, False))
            self.assertIn("worker_recovery_exhausted", qa["flags"])
            self.assertEqual(qa["free_retry_count"], 2)
            if store is self.sql:
                self.assertEqual(PersistentJobStore(self.engine).get("book").translation_stats, stats)

    def test_exhaustion_marks_paid_precision_for_review_not_as_refunded(self):
        for store in self.stores:
            for test_order in (False, True):
                key = "test" if test_order else "paid"
                store.add(self.job(key, enable_precision_polish=True, is_test_order=test_order,
                    translation_stats={"attempt_id": "attempt", "cached_chunks": 19,
                        "precision_polish": {"status": "running", "quoted_amount": "6.00",
                                             "reviewed": 23, "api_calls": 8, "validation_passed": True}}))
                self.begin(store, key)
                self.assertEqual(self.recover(store, key, cap=0), "exhausted")
                job = store.get(key)
                precision = job.translation_stats["precision_polish"]
                self.assertEqual((precision["status"], precision["reason"], precision["validation_passed"]),
                                 ("failed", "worker_recovery_exhausted", False))
                self.assertEqual(precision["refund_required"], not test_order)
                self.assertEqual((precision["quoted_amount"], precision["reviewed"], precision["api_calls"]), ("6.00", 23, 8))
                self.assertNotIn("refunded", precision)
                if store is self.sql:
                    self.assertEqual(PersistentJobStore(self.engine).get(key).translation_stats, job.translation_stats)
                    with self.engine.connect() as connection:
                        self.assertEqual(connection.execute(text("SELECT precision_polish_status FROM epub_jobs WHERE id=:id"), {"id": key}).scalar(), "failed")

    def test_fresh_heartbeat_stale_owner_and_changed_attempt_are_untouched(self):
        for store in self.stores:
            store.add(self.job()); self.begin(store)
            self.assertEqual(self.recover(store, owner="wrong"), "unchanged")
            store.heartbeat_execution("book", "attempt", "owner", now=201)
            self.assertEqual(self.recover(store, cutoff=200), "unchanged")
            store.update_status("book", JobStatus.running, translation_stats={"attempt_id": "new"})
            self.assertEqual(self.recover(store, cutoff=999), "unchanged")
            self.assertFalse(store.heartbeat_execution("book", "attempt", "owner"))
            self.assertEqual(store.get("book").translation_stats["attempt_id"], "new")

    def test_concurrent_recovery_claims_requeue_only_once(self):
        for store in self.stores:
            store.add(self.job()); self.begin(store)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.recover(store), range(12)))
            self.assertEqual(results.count("recovered"), 1)
            self.assertEqual(results.count("unchanged"), 11)
            self.assertEqual(store.get_execution("book", "attempt")["recoveries"], 1)

    def test_recovery_and_fresh_heartbeat_are_serialized(self):
        for store in self.stores:
            store.add(self.job()); self.begin(store)
            barrier = threading.Barrier(2)
            def heartbeat():
                barrier.wait()
                return store.heartbeat_execution("book", "attempt", "owner", now=250)
            def recover():
                barrier.wait()
                return self.recover(store)
            with ThreadPoolExecutor(max_workers=2) as pool:
                heart, recovery = pool.submit(heartbeat), pool.submit(recover)
                heartbeat_won, recovered = heart.result(), recovery.result()
            self.assertEqual((heartbeat_won, recovered) in {(True, "unchanged"), (False, "recovered")}, True)

    def test_legacy_empty_attempt_is_recovered_without_rewriting_stats(self):
        for store in self.stores:
            store.add(self.job(status=JobStatus.running, enable_translation=True,
                               translation_stats={"cached_chunks": 31}))
            row = store.list_stale_executions(stale_before=200)[0]
            self.assertEqual((row["attempt_id"], row["owner"]), ("", ""))
            self.assertEqual(store.recover_execution("book", "", "", stale_before=200, now=300), "recovered")
            self.assertEqual(store.get("book").translation_stats, {"cached_chunks": 31})
            self.assertEqual(store.list_dispatches()[0]["attempt_id"], "")
            self.assertEqual(store.get_execution("book", "")["recoveries"], 1)

    def test_sql_execution_insertion_failure_rolls_back_begin_and_recovery(self):
        def fail(*_args):
            raise RuntimeError("injected execution write failure")
        self.sql.add(self.job())
        self.sql.add(self.job("legacy", status=JobStatus.running))
        event.listen(ExecutionRecord, "before_insert", fail)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.begin(self.sql)
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.sql.recover_execution("legacy", "attempt", "", stale_before=200, now=300)
        finally:
            event.remove(ExecutionRecord, "before_insert", fail)
        self.assertEqual(self.sql.get("book").status, JobStatus.pending)
        self.assertIsNone(self.sql.get_execution("book", "attempt"))
        self.assertEqual(self.sql.get("legacy").status, JobStatus.running)
        self.assertEqual(self.sql.list_dispatches("legacy"), [])

    def test_legacy_stable_identity_migration_preserves_cap_and_obsoletes_empty_delivery(self):
        for store in self.stores:
            for kind, options in (("translation", {"enable_translation": True}),
                                  ("polish", {"enable_precision_polish": True})):
                store.add(self.job(kind, status=JobStatus.running, translation_stats={"cached_chunks": 9}, **options))
                self.assertEqual(store.recover_execution(kind, "", "", stale_before=200, now=300, max_recoveries=1), "recovered")
                stable = f"{kind}-{kind}"
                store.update_status(kind, JobStatus.pending, translation_stats={"attempt_id": stable})
                self.assertTrue(store.begin_execution(kind, stable, "new-owner", now=310))
                self.assertEqual(store.get_execution(kind, stable)["recoveries"], 1)
                self.assertEqual(store.get_execution(kind, "")["state"], "finished")
                self.assertEqual(store.get_dispatch(dispatch_identity(kind, ""))["status"], "obsolete")
                self.assertEqual(store.recover_execution(kind, stable, "new-owner", stale_before=400, now=410, max_recoveries=1), "exhausted")
                self.assertEqual(store.get(kind).translation_stats["cached_chunks"], 9)

    def test_explicit_new_retry_does_not_inherit_legacy_recovery_count(self):
        for store in self.stores:
            store.add(self.job(status=JobStatus.running, enable_translation=True, translation_stats={}))
            store.recover_execution("book", "", "", stale_before=200, now=300)
            store.update_status("book", JobStatus.pending, translation_stats={"attempt_id": "user-requested-new-attempt"})
            self.assertTrue(store.begin_execution("book", "user-requested-new-attempt", "new-owner", now=310))
            self.assertEqual(store.get_execution("book", "user-requested-new-attempt")["recoveries"], 0)
            self.assertEqual(store.get_execution("book", "")["state"], "queued")

    def test_memory_recovery_preparation_failure_mutates_nothing(self):
        self.memory.add(self.job()); self.begin(self.memory)
        before = self.memory.get_execution("book", "attempt")
        with patch("app.storage.recovery_outbox", side_effect=RuntimeError("injected")), self.assertRaisesRegex(RuntimeError, "injected"):
            self.recover(self.memory)
        self.assertEqual(self.memory.get_execution("book", "attempt"), before)
        self.assertEqual(self.memory.get("book").status, JobStatus.running)

    def test_additive_table_and_restart_preserve_records_without_job_schema_changes(self):
        self.sql.add(self.job()); self.begin(self.sql)
        with self.engine.begin() as connection:
            columns_before = connection.execute(text("PRAGMA table_info(epub_jobs)")).all()
        Base.metadata.create_all(self.engine)
        reopened = PersistentJobStore(self.engine)
        self.assertEqual(reopened.get_execution("book", "attempt"), self.sql.get_execution("book", "attempt"))
        self.assertEqual(self.recover(reopened), "recovered")
        with self.engine.begin() as connection:
            self.assertEqual(connection.execute(text("PRAGMA table_info(epub_jobs)")).all(), columns_before)

    def test_limit_cap_and_identity_inputs_fail_closed(self):
        for store in self.stores:
            store.add(self.job())
            for bad in (True, -1, 1.5, "2", None, 11):
                with self.assertRaises(ValueError):
                    self.recover(store, cap=bad)
            for bad in (True, 0, -1, 1.5, "2", None, 101):
                with self.assertRaises(ValueError):
                    store.list_stale_executions(stale_before=200, limit=bad)
            for attempt, owner in ((None, "owner"), ("attempt", ""), ("attempt", " ")):
                with self.assertRaises(ValueError):
                    store.begin_execution("book", attempt, owner)
            self.assertEqual(store.get("book").status, JobStatus.pending)


class ExecutionExhaustedPublicTests(unittest.TestCase):
    """Expose persisted terminal QA through real HTTP, without running startup."""
    import test_d43_dispatch_contract as fixtures
    _patch = fixtures.DispatchContractTests._patch
    tearDown = fixtures.DispatchContractTests.tearDown
    job = fixtures.DispatchContractTests.job

    def setUp(self):
        self.fixtures.DispatchContractTests.setUp(self)
        self.client.app.add_api_route("/jobs/{job_id}", self.main.get_job_v2, methods=["GET"])

    def test_reloaded_failed_detail_never_exposes_old_passed_or_running_metadata(self):
        for key, flags in (("translation", {"enable_translation": True}),
                           ("polish", {"enable_precision_polish": True})):
            self.job(key, status=self.Status.pending, **flags, translation_stats={
                "attempt_id": "attempt", "cached_chunks": 17, "live": True, "deliverable": True,
                "qa_report": {"status": "passed", "can_deliver": True},
                "precision_polish": {"status": "running", "quoted_amount": "6.00", "api_calls": 4},
            })
            self.assertTrue(self.store.begin_execution(key, "attempt", "worker", now=100))
            self.assertEqual(self.store.recover_execution(key, "attempt", "worker", stale_before=200, now=300,
                                                         max_recoveries=0), "exhausted")
            with patch.object(self.main, "job_store", self.Store(self.engine)):
                response = self.client.get("/jobs/" + key, headers={"X-Job-Token": "owner-only"})
            self.assertEqual(response.status_code, 200, response.text)
            public = response.json()
            self.assertEqual(public["error_code"], "WORKER_RECOVERY_EXHAUSTED")
            self.assertIsNone(public["download_url"])
            stats = public["translation_stats"]
            self.assertFalse(stats["live"])
            self.assertFalse(stats["deliverable"])
            self.assertEqual(stats["qa_report"]["status"], "failed")
            self.assertFalse(stats["qa_report"]["can_deliver"])
            self.assertEqual(stats["cached_chunks"], 17)
            if key == "polish":
                self.assertEqual(public["precision_polish"]["status"], "failed")
                self.assertTrue(public["precision_polish"]["refund_required"])
                self.assertEqual(public["precision_polish"]["quoted_amount"], "6.00")
                self.assertEqual(public["precision_polish"]["api_calls"], 4)
                self.assertNotIn("refunded", public["precision_polish"])


if __name__ == "__main__":
    unittest.main()
