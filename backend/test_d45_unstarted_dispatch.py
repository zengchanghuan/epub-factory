"""R7 sent-but-never-started delivery retries, without counting worker loss."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import unittest

import test_d45_execution_store as existing
from app.models import JobStatus
from app.domain.dispatch_intent import dispatch_identity, timestamp


class UnstartedDispatchTests(unittest.TestCase):
    setUp = existing.ExecutionStoreTests.setUp
    job = existing.ExecutionStoreTests.job

    def sent(self, store, key="book", *, at=None):
        at = timestamp() + 1 if at is None else at
        claim = store.claim_dispatch(job_id=key, now=at)
        self.assertIsNotNone(claim)
        self.assertTrue(store.finish_dispatch(claim["dispatch_id"], claim["lease_token"], now=at))
        return store.get_dispatch(claim["dispatch_id"])

    def test_long_grace_then_rearm_preserves_job_stats_and_execution_counts(self):
        for store in self.stores:
            store.add(self.job())
            row = self.sent(store)
            before = store.get("book")
            stats, updated = dict(before.translation_stats), before.updated_at
            self.assertEqual(store.list_unstarted_dispatches(now=row["updated_at"] + 3599), [])
            self.assertEqual(len(store.list_unstarted_dispatches(now=row["updated_at"] + 3600)), 1)
            self.assertTrue(store.rearm_unstarted_dispatch("book", "attempt", sent_at=row["updated_at"], now=row["updated_at"] + 3600))
            self.assertEqual(store.get("book").translation_stats, stats)
            self.assertEqual(store.get("book").updated_at, updated)
            self.assertEqual(store.get("book").status, JobStatus.pending)
            self.assertIsNone(store.get_execution("book", "attempt"))
            self.assertEqual(store.get_dispatch(row["dispatch_id"])["status"], "pending")

    def test_existing_recovery_budget_is_not_spent_by_queue_wait(self):
        for store in self.stores:
            store.add(self.job())
            store.begin_execution("book", "attempt", "old", now=100)
            store.recover_execution("book", "attempt", "old", stale_before=200, now=300)
            execution = store.get_execution("book", "attempt")
            row = self.sent(store)
            self.assertTrue(store.rearm_unstarted_dispatch("book", "attempt", sent_at=row["updated_at"], now=row["updated_at"] + 3600))
            self.assertEqual(store.get_execution("book", "attempt"), execution)

    def test_repeated_unstarted_redelivery_uses_exponential_grace_capped_at_eight(self):
        for store in self.stores:
            store.add(self.job())
            at = timestamp() + 1
            for multiplier in (1, 2, 4, 8, 8):
                row = self.sent(store, at=at)
                due = at + 600 * multiplier
                self.assertEqual(store.list_unstarted_dispatches(now=due - 1, grace_seconds=600), [])
                self.assertFalse(store.rearm_unstarted_dispatch("book", "attempt", sent_at=at, now=due - 1, grace_seconds=600))
                self.assertTrue(store.rearm_unstarted_dispatch("book", "attempt", sent_at=at, now=due, grace_seconds=600))
                at = due
            self.assertIsNone(store.get_execution("book", "attempt"))

    def test_concurrent_rearm_only_one_wins_same_sent_version(self):
        for store in self.stores:
            store.add(self.job())
            row = self.sent(store)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: store.rearm_unstarted_dispatch("book", "attempt", sent_at=row["updated_at"], now=row["updated_at"] + 3600), range(12)))
            self.assertEqual(sum(results), 1)

    def test_republished_version_rejects_old_scanner_snapshot(self):
        for store in self.stores:
            store.add(self.job())
            first = self.sent(store)
            at = first["updated_at"] + 3600
            store.rearm_unstarted_dispatch("book", "attempt", sent_at=first["updated_at"], now=at)
            second = self.sent(store, at=at)
            self.assertFalse(store.rearm_unstarted_dispatch("book", "attempt", sent_at=first["updated_at"], now=at + 99999))
            self.assertEqual(store.get_dispatch(second["dispatch_id"]), second)

    def test_fresh_publishing_and_never_sent_intents_are_not_rearmed(self):
        for store in self.stores:
            store.add(self.job())
            at = timestamp() + 1
            self.assertEqual(store.list_unstarted_dispatches(now=at + 99999), [])
            publishing = store.claim_dispatch(now=at)
            self.assertEqual(store.list_unstarted_dispatches(now=at + 99999), [])
            self.assertFalse(store.rearm_unstarted_dispatch("book", "attempt", sent_at=at, now=at + 99999))
            self.assertEqual(store.get_dispatch(publishing["dispatch_id"]), publishing)

    def test_cancel_terminal_unpaid_running_and_new_attempt_are_excluded(self):
        for store in self.stores:
            for status in (JobStatus.cancelled, JobStatus.failed, JobStatus.success, JobStatus.running,
                           JobStatus.pending_payment, JobStatus.awaiting_confirmation):
                key = status.value
                store.add(self.job(key))
                row = self.sent(store, key)
                store.update_status(key, status)
                self.assertFalse(store.rearm_unstarted_dispatch(key, "attempt", sent_at=row["updated_at"], now=row["updated_at"] + 99999))
            store.add(self.job("new"))
            row = self.sent(store, "new")
            store.update_status("new", JobStatus.pending, translation_stats={"attempt_id": "new-attempt"})
            self.assertFalse(store.rearm_unstarted_dispatch("new", "attempt", sent_at=row["updated_at"], now=row["updated_at"] + 99999))
            self.assertEqual(store.list_unstarted_dispatches(now=row["updated_at"] + 99999), [])

    def test_worker_begin_wins_over_delayed_scanner(self):
        for store in self.stores:
            store.add(self.job())
            row = self.sent(store)
            self.assertTrue(store.begin_execution("book", "attempt", "worker", now=row["updated_at"] + 3600))
            self.assertFalse(store.rearm_unstarted_dispatch("book", "attempt", sent_at=row["updated_at"], now=row["updated_at"] + 3601))
            self.assertEqual(store.get_execution("book", "attempt")["recoveries"], 0)

    def test_bounded_sorted_copies_and_argument_validation(self):
        for store in self.stores:
            at = timestamp() + 1
            for index in range(3):
                store.add(self.job(str(index)))
                self.sent(store, str(index), at=at + index)
            rows = store.list_unstarted_dispatches(now=datetime.fromtimestamp(at + 5000, timezone.utc), limit=2)
            self.assertEqual([row["job_id"] for row in rows], ["0", "1"])
            rows[0]["status"] = "modified"
            self.assertEqual(store.get_dispatch(dispatch_identity("0", "attempt"))["status"], "sent")
            for bad in (True, 599, 86401, "3600", 3600.5):
                with self.assertRaises(ValueError):
                    store.list_unstarted_dispatches(grace_seconds=bad)
                with self.assertRaises(ValueError):
                    store.rearm_unstarted_dispatch("0", "attempt", sent_at=at, grace_seconds=bad)
            for bad in (0, 101, True, 1.5):
                with self.assertRaises(ValueError):
                    store.list_unstarted_dispatches(limit=bad)


if __name__ == "__main__":
    unittest.main()
