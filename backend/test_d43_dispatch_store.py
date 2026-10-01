"""R5 dispatch outbox parity and transaction tests. No network or user files."""
import os
import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, text

with patch.dict(os.environ, {}, clear=True):
    from app.storage import JobStore
from app.storage_db import Base, DispatchRecord, PersistentJobStore
from app.domain.dispatch_intent import build_dispatch_intent, dispatch_identity, timestamp
from app.domain.payment_entitlement import quote_entitlement
from app.models import Job, JobStatus, OutputMode


class DispatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")))
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.engine = create_engine(f"sqlite:///{self.root / 'jobs.db'}", connect_args={"check_same_thread": False})
        self.stack.callback(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.memory = JobStore()
        self.sql = PersistentJobStore(self.engine)
        self.stores = (self.memory, self.sql)

    def job(self, name="book", **values):
        fields = dict(id=name, source_filename="fixture.epub", input_path="/not-opened/fixture.epub",
                      trace_id="offline", output_mode=OutputMode.simplified, status=JobStatus.pending_payment)
        fields.update(values)
        return Job(**fields)

    def test_pending_creation_and_first_attempt_identity_are_atomic_and_idempotent(self):
        for store in self.stores:
            for name, options in (("ordinary", {}), ("translation", {"enable_translation": True}),
                                  ("polish", {"enable_precision_polish": True})):
                with self.subTest(store=type(store).__name__, name=name):
                    job = self.job(name, status=JobStatus.pending, translation_stats={"kept": 7}, **options)
                    store.add(job)
                    first = store.list_dispatches(name)[0]
                    self.assertEqual(first, store.ensure_dispatch(name))
                    self.assertEqual(first, store.ensure_dispatch(name))
                    self.assertEqual(len(store.list_dispatches(name)), 1)
                    self.assertEqual(first["attempt_id"], job.translation_stats.get("attempt_id", ""))
                    self.assertEqual(bool(first["attempt_id"]), bool(options))
                    self.assertEqual(store.get(name).translation_stats["kept"], 7)

    def test_unpaid_active_and_terminal_jobs_have_no_implicit_dispatch(self):
        for store in self.stores:
            for status in JobStatus:
                if status == JobStatus.pending:
                    continue
                store.add(self.job(status.value, status=status))
                self.assertIsNone(store.ensure_dispatch(status.value))
            self.assertIsNone(store.ensure_dispatch("missing"))
            self.assertEqual(store.list_dispatches(), [])
            self.assertIsNone(store.claim_dispatch())

    def test_paid_transition_has_one_intent_under_duplicate_concurrent_callbacks(self):
        for store in self.stores:
            store.add(self.job(enable_translation=True))
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: store.try_mark_paid("book"), range(16)))
            self.assertEqual(sum(results), 1)
            self.assertEqual(store.get("book").status, JobStatus.pending)
            intents = store.list_dispatches("book")
            self.assertEqual(len(intents), 1)
            self.assertTrue(intents[0]["attempt_id"])

    def test_batch_transaction_only_unlocks_previously_unpaid_members(self):
        for store in self.stores:
            for index in range(3):
                store.add(self.job(str(index), batch_id="batch", batch_index=index, batch_size=3))
            store.add(self.job("cancelled", batch_id="batch", batch_index=3, status=JobStatus.cancelled))
            self.assertTrue(store.try_mark_batch_paid("batch"))
            self.assertFalse(store.try_mark_batch_paid("batch"))
            self.assertEqual({row["job_id"] for row in store.list_dispatches()}, {"0", "1", "2"})
            self.assertEqual(store.get("cancelled").status, JobStatus.cancelled)

    def test_batch_without_leader_cannot_unlock(self):
        for store in self.stores:
            store.add(self.job("orphan", batch_id="batch", batch_index=1))
            self.assertFalse(store.try_mark_batch_paid("batch"))
            self.assertEqual(store.get("orphan").status, JobStatus.pending_payment)
            self.assertEqual(store.list_dispatches(), [])

    def test_concurrent_batch_callbacks_unlock_once_with_all_intents(self):
        for store in self.stores:
            for index in range(4):
                store.add(self.job(str(index), batch_id="batch", batch_index=index, enable_translation=True))
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: store.try_mark_batch_paid("batch"), range(12)))
            self.assertEqual(sum(results), 1)
            self.assertEqual(len(store.list_dispatches()), 4)
            self.assertEqual({row["job_id"] for row in store.list_dispatches()}, {"0", "1", "2", "3"})

    def test_claim_has_single_owner_under_concurrency(self):
        for store in self.stores:
            store.add(self.job(status=JobStatus.pending))
            now = timestamp() + 1
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: store.claim_dispatch(now=now), range(16)))
            claimed = [row for row in results if row]
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["attempts"], 1)
            self.assertTrue(claimed[0]["lease_token"])

    def test_expired_lease_recovers_and_stale_owner_cannot_ack(self):
        for store in self.stores:
            store.add(self.job(status=JobStatus.pending))
            at = datetime.now(timezone.utc).timestamp() + 1
            first = store.claim_dispatch(now=at, lease_seconds=5)
            self.assertIsNone(store.claim_dispatch(now=at + 4))
            second = store.claim_dispatch(now=datetime.fromtimestamp(at + 5, timezone.utc))
            self.assertNotEqual(first["lease_token"], second["lease_token"])
            self.assertEqual(second["attempts"], 2)
            self.assertFalse(store.finish_dispatch(first["dispatch_id"], first["lease_token"]))
            self.assertTrue(store.finish_dispatch(second["dispatch_id"], second["lease_token"], outcome="sent", now=at + 6))
            self.assertFalse(store.finish_dispatch(second["dispatch_id"], second["lease_token"], outcome="retry"))
            self.assertIsNone(store.claim_dispatch(now=at + 1000))

    def test_retry_backoff_is_persisted_across_new_store_instance(self):
        for store in self.stores:
            store.add(self.job(status=JobStatus.pending))
            at = timestamp() + 1
            first = store.claim_dispatch(now=at)
            self.assertTrue(store.finish_dispatch(first["dispatch_id"], first["lease_token"], outcome="retry",
                                                 error="broker unavailable", retry_delay_seconds=8, now=at + 1))
            reopened = PersistentJobStore(self.engine) if store is self.sql else store
            self.assertIsNone(reopened.claim_dispatch(now=at + 8))
            again = reopened.claim_dispatch(now=at + 9)
            self.assertEqual(again["attempts"], 2)
            self.assertEqual(again["last_error"], "broker unavailable")

    def test_only_current_owner_can_finish_and_outcomes_are_validated(self):
        for store in self.stores:
            store.add(self.job(status=JobStatus.pending))
            row = store.claim_dispatch(now=timestamp() + 1)
            self.assertFalse(store.finish_dispatch(row["dispatch_id"], ""))
            self.assertFalse(store.finish_dispatch(row["dispatch_id"], "wrong"))
            with self.assertRaises(ValueError):
                store.finish_dispatch(row["dispatch_id"], row["lease_token"], outcome="unsupported")
            self.assertTrue(store.finish_dispatch(row["dispatch_id"], row["lease_token"], outcome="obsolete"))
            self.assertEqual(store.list_dispatches()[0]["status"], "obsolete")

    def test_confirmed_bypass_transition_records_intent_but_payment_wait_does_not(self):
        for store in self.stores:
            for name, status in (("bypass", JobStatus.pending), ("paid", JobStatus.pending_payment)):
                store.add(self.job(name, status=JobStatus.confirming, enable_translation=True))
                result = store.finish_translation_confirmation(name, status=status, message="fixture", expected_amount="1.00")
                self.assertEqual(result.status, status)
            self.assertEqual([row["job_id"] for row in store.list_dispatches()], ["bypass"])

    def test_authorized_retry_creates_distinct_attempt_intent_in_same_transition(self):
        for store in self.stores:
            job = self.job(status=JobStatus.pending, enable_translation=True, is_test_order=True)
            job.payment_entitlement = quote_entitlement(job, test_bypass=True)
            store.add(job)
            first = store.list_dispatches()[0]
            store.update_status("book", JobStatus.failed)
            retried, reason = store.restart_translation_attempt("book", attempt_id="retry-2", action_label="retry",
                max_free_retries=-1, started_at=datetime.now(timezone.utc))
            self.assertEqual(reason, "ok")
            self.assertEqual(retried.status, JobStatus.pending)
            self.assertEqual({row["attempt_id"] for row in store.list_dispatches()}, {first["attempt_id"], "retry-2"})
            self.assertEqual(len({row["dispatch_id"] for row in store.list_dispatches()}), 2)
            current_id = dispatch_identity("book", "retry-2")
            self.assertEqual(store.get_dispatch(current_id)["attempt_id"], "retry-2")
            self.assertEqual(store.get_dispatch(first["dispatch_id"])["attempt_id"], first["attempt_id"])
            self.assertIsNone(store.get_dispatch(dispatch_identity("book", "not-created")))

    def test_denied_retry_creates_no_intent(self):
        for store in self.stores:
            store.add(self.job(status=JobStatus.failed, enable_translation=True))
            _, reason = store.restart_translation_attempt("book", attempt_id="not-authorized", action_label="retry",
                max_free_retries=-1, started_at=datetime.now(timezone.utc))
            self.assertEqual(reason, "payment_review_required")
            self.assertEqual(store.list_dispatches(), [])

    def test_sql_insertion_failure_rolls_back_add_paid_batch_confirmation_and_retry(self):
        def fail(_mapper, _connection, _target):
            raise RuntimeError("injected outbox insertion failure")
        for name in ("paid", "batch-0", "batch-1", "confirm", "retry"):
            self.sql.add(self.job(name,
                status=JobStatus.confirming if name == "confirm" else JobStatus.failed if name == "retry" else JobStatus.pending_payment,
                batch_id="batch" if name.startswith("batch-") else "",
                batch_index=int(name[-1]) if name.startswith("batch-") else 0))
        event.listen(DispatchRecord, "before_insert", fail)
        try:
            actions = (
                lambda: self.sql.add(self.job("add", status=JobStatus.pending)),
                lambda: self.sql.try_mark_paid("paid"),
                lambda: self.sql.try_mark_batch_paid("batch"),
                lambda: self.sql.finish_translation_confirmation("confirm", status=JobStatus.pending, message="ok", expected_amount="0"),
                lambda: self.sql.restart_translation_attempt("retry", attempt_id="retry-2", action_label="retry", max_free_retries=-1, started_at=datetime.now(timezone.utc)),
            )
            for action in actions:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    action()
            self.assertIsNone(self.sql.get("add"))
            self.assertEqual(self.sql.get("paid").status, JobStatus.pending_payment)
            self.assertEqual(self.sql.get("batch-0").status, JobStatus.pending_payment)
            self.assertEqual(self.sql.get("batch-1").status, JobStatus.pending_payment)
            self.assertEqual(self.sql.get("confirm").status, JobStatus.confirming)
            self.assertEqual(self.sql.get("retry").status, JobStatus.failed)
            self.assertEqual(self.sql.list_dispatches(), [])
        finally:
            event.remove(DispatchRecord, "before_insert", fail)

    def test_memory_preparation_failure_rolls_back_whole_batch(self):
        for index in range(2):
            self.memory.add(self.job(str(index), batch_id="batch", batch_index=index))
        calls = 0
        def fail_second(job, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected preparation failure")
            return build_dispatch_intent(job, **kwargs)
        with patch("app.storage.build_dispatch_intent", side_effect=fail_second), self.assertRaisesRegex(RuntimeError, "injected"):
            self.memory.try_mark_batch_paid("batch")
        self.assertEqual([self.memory.get(str(i)).status for i in range(2)], [JobStatus.pending_payment] * 2)
        self.assertEqual(self.memory.list_dispatches(), [])

    def test_memory_preparation_failure_rolls_back_single_transitions(self):
        for name, status in (("paid", JobStatus.pending_payment), ("confirm", JobStatus.confirming),
                             ("retry", JobStatus.failed)):
            self.memory.add(self.job(name, status=status, translation_stats={"kept": 17}))
        with patch("app.storage.build_dispatch_intent", side_effect=RuntimeError("injected")):
            actions = (
                lambda: self.memory.add(self.job("add", status=JobStatus.pending)),
                lambda: self.memory.try_mark_paid("paid"),
                lambda: self.memory.finish_translation_confirmation("confirm", status=JobStatus.pending, message="ok", expected_amount="0"),
                lambda: self.memory.restart_translation_attempt("retry", attempt_id="next", action_label="retry", max_free_retries=-1, started_at=datetime.now(timezone.utc)),
            )
            for action in actions:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    action()
        self.assertIsNone(self.memory.get("add"))
        for name, status in (("paid", JobStatus.pending_payment), ("confirm", JobStatus.confirming),
                             ("retry", JobStatus.failed)):
            self.assertEqual(self.memory.get(name).status, status)
            self.assertEqual(self.memory.get(name).translation_stats, {"kept": 17})
        self.assertEqual(self.memory.list_dispatches(), [])

    def test_ensure_does_not_rearm_sent_intent_or_clear_backoff(self):
        for store in self.stores:
            store.add(self.job(status=JobStatus.pending))
            first = store.claim_dispatch(now=timestamp() + 1)
            store.finish_dispatch(first["dispatch_id"], first["lease_token"], outcome="retry", retry_delay_seconds=99)
            backed_off = store.list_dispatches()[0]
            self.assertEqual(store.ensure_dispatch("book"), backed_off)
            claimed = store.claim_dispatch(now=backed_off["next_attempt_at"])
            store.finish_dispatch(claimed["dispatch_id"], claimed["lease_token"])
            sent = store.list_dispatches()[0]
            self.assertEqual(sent["status"], "sent")
            self.assertEqual(store.ensure_dispatch("book"), sent)

    def test_explicit_legacy_recovery_is_concurrent_idempotent_and_not_automatic(self):
        # Emulate an old paid-pending row without an intent. Construction and
        # claims must not silently backfill it; caller explicitly authorizes.
        for store in self.stores:
            store.add(self.job(enable_translation=True))
            store.update_status("book", JobStatus.pending)
            self.assertEqual(store.list_dispatches(), [])
            self.assertIsNone(store.claim_dispatch())
            with ThreadPoolExecutor(max_workers=8) as pool:
                recovered = list(pool.map(lambda _: store.ensure_dispatch("book"), range(8)))
            self.assertEqual(len({row["dispatch_id"] for row in recovered}), 1)
            self.assertEqual(len(store.list_dispatches()), 1)

    def test_table_creation_is_additive_and_preserves_existing_jobs(self):
        self.sql.add(self.job("original", status=JobStatus.failed, translation_stats={"kept": 41}))
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE job_dispatch_outbox"))
        Base.metadata.create_all(self.engine)
        self.assertEqual(self.sql.get("original").translation_stats, {"kept": 41})
        self.assertEqual(self.sql.list_dispatches(), [])

    def test_public_records_are_copies_and_identity_is_unambiguous(self):
        self.assertNotEqual(dispatch_identity("a:b", "c"), dispatch_identity("a", "b:c"))
        self.assertEqual(timestamp(datetime(2026, 1, 1)), timestamp(datetime(2026, 1, 1, tzinfo=timezone.utc)))
        for store in self.stores:
            store.add(self.job(status=JobStatus.pending))
            row = store.ensure_dispatch("book")
            row["status"] = "sent"
            self.assertEqual(store.list_dispatches()[0]["status"], "pending")
            lookup = store.get_dispatch(row["dispatch_id"])
            lookup["status"] = "obsolete"
            self.assertEqual(store.get_dispatch(row["dispatch_id"])["status"], "pending")
            self.assertIsNone(store.get_dispatch("missing"))
            self.assertEqual(store.list_dispatches(limit=0), [])
            self.assertIsNone(store.claim_dispatch(job_id="other"))


if __name__ == "__main__":
    unittest.main()
