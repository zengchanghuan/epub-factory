"""C PDF lifecycle store contracts: detached memory + real temporary SQLite.

Plans and bytes are synthetic test data; no real parser, provider or customer
file is used. Every transition retains the original quote and paid boundary.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.orm import Session

import test_d45_execution_store as fixtures
from app.models import JobStatus, OutputMode
from app.storage_db import DispatchRecord, ExecutionRecord, PersistentJobStore
from app.domain.pdf_product import new_pdf_plan, pdf_plan_identity, validate_pdf_plan
from app.domain.checkout_resume import checkout_snapshot
from app.domain.payment_entitlement import grant_test_entitlement


def prepared_plan(*, source_sha256="a" * 64, source_bytes=123, amount="1.99"):
    plan = {**new_pdf_plan(source_sha256, source_bytes, amount), "phase": "prepared",
            "artifact_id": "b" * 32, "artifact_sha256": "c" * 64, "artifact_bytes": 456,
            "versions": {"parser": "test-v1", "package": "test-v1", "guard": "test-v1",
                         "parser_sha256": "d" * 64, "images_sha256": "e" * 64,
                         "validator_sha256": "f" * 64, "pypdf": "6.0.0", "pdfplumber": "0.11.9",
                         "pillow": "11.0.0", "epubcheck": "5.1.0", "epubcheck_sha256": "1" * 64},
            "report": {"page_count": 1, "normalized_characters": 10, "zero_width_spaces_preserved": 0,
                       "image_assets": 0, "image_placements": 0, "toc_entries": 1, "paragraph_count": 1,
                       "warnings": [], "memory_limited": True, "validation_passed": True,
                       "epubcheck_warnings": 0, "requires_review": False, "eligible_for_payment": True}}
    plan["plan_id"] = pdf_plan_identity(plan)
    return validate_pdf_plan(plan, phase="prepared")


def confirmed_plan(plan):
    return {**deepcopy(plan), "phase": "confirmed", "accepted_warnings": list(plan["report"]["warnings"]),
            "confirmed_at": datetime.now(timezone.utc).isoformat()}


class PdfStoreTests(unittest.TestCase):
    setUp = fixtures.ExecutionStoreTests.setUp

    def job(self, key="book", **values):
        defaults = {"source_filename": "synthetic.pdf", "input_path": "/unused/synthetic.pdf",
                    "output_mode": OutputMode.original, "expected_amount": "1.99",
                    "translation_stats": {"attempt_id": "preparation", "preserved": {"counter": 7},
                                          "pdf_conversion": new_pdf_plan("a" * 64, 123, "1.99")}}
        defaults.update(values)
        return fixtures.ExecutionStoreTests.job(self, key, **defaults)

    def start(self, store, key="book", **values):
        store.add(self.job(key, **values))
        self.assertTrue(store.begin_execution(key, "preparation", "owner"))

    def prepare(self, store, key="book", **values):
        self.start(store, key, **values)
        saved = store.finish_pdf_preparation(key, "preparation", "owner", prepared_plan())
        self.assertIsNotNone(saved)
        return saved

    def claim(self, store, key="book"):
        plan = store.get(key).translation_stats["pdf_conversion"]
        return store.begin_pdf_confirmation(key, plan_id=plan["plan_id"], confirmed_plan=confirmed_plan(plan))

    def atomic_confirm(self, store, key="book", *, test=False, **overrides):
        plan = prepared_plan()
        values = dict(plan_id=plan["plan_id"], confirmed_plan=confirmed_plan(plan),
                      status=JobStatus.pending if test else JobStatus.pending_payment,
                      message="confirmed", allow_test_bypass=test,
                      payment_checkout=None if test else checkout_snapshot(
                          key, "1.99", pay_url="https://pay.example.invalid/original",
                          created_at=datetime.now(timezone.utc)))
        values.update(overrides)
        return store.confirm_pdf_conversion(key, **values)

    def test_atomic_confirmation_saves_checkout_new_attempt_without_intermediate_state(self):
        for store in self.stores:
            prepared = self.prepare(store)
            saved = self.atomic_confirm(store)
            self.assertEqual(saved.status, JobStatus.pending_payment)
            self.assertEqual(saved.translation_stats["pdf_conversion"]["phase"], "confirmed")
            self.assertNotEqual(saved.translation_stats["attempt_id"], prepared.translation_stats["attempt_id"])
            self.assertEqual(saved.translation_stats["preserved"], {"counter": 7})
            self.assertEqual(saved.expected_amount, "1.99")
            self.assertEqual(saved.translation_stats["payment_checkout"]["amount"], "1.99")
            self.assertIsNone(saved.output_path)
            self.assertEqual(len(store.list_dispatches("book")), 1)
            self.assertIsNone(self.atomic_confirm(store))
            self.assertEqual(store.get("book"), saved)
            self.assertIsNone(store.rollback_pdf_confirmation(
                "book", attempt_id=saved.translation_stats["attempt_id"], message="late error"))
            saved.translation_stats["pdf_conversion"]["report"]["page_count"] = 99
            self.assertEqual(store.get("book").translation_stats["pdf_conversion"]["report"]["page_count"], 1)
        self.assertEqual(PersistentJobStore(self.engine).get("book"), self.sql.get("book"))

    def test_atomic_server_test_commits_entitlement_and_single_delivery_intent(self):
        for store in self.stores:
            self.prepare(store, is_test_order=True)
            saved = self.atomic_confirm(store, test=True)
            self.assertEqual(saved.status, JobStatus.pending)
            entitlement = saved.payment_entitlement
            self.assertEqual((entitlement["version"], entitlement["state"], entitlement["source"]),
                             (1, "test_authorized", "server_test_bypass"))
            self.assertEqual((entitlement["order_no"], entitlement["amount"]), ("book", "1.99"))
            self.assertIsNotNone(datetime.fromisoformat(entitlement["authorized_at"]).tzinfo)
            self.assertNotIn("payment_checkout", saved.translation_stats)
            attempt = saved.translation_stats["attempt_id"]
            self.assertEqual([row["attempt_id"] for row in store.list_dispatches("book")].count(attempt), 1)
            self.assertIsNone(self.atomic_confirm(store, test=True))
            self.assertEqual(store.get("book"), saved)
            self.assertEqual(len(store.list_dispatches("book")), 2)

    def test_atomic_test_authority_is_strict_and_never_inferred_from_order_flag(self):
        for store in self.stores:
            self.prepare(store, is_test_order=True)
            before = store.get("book")
            for value in (False, 0, 1, "true", None, [], {}):
                self.assertIsNone(self.atomic_confirm(store, test=True, allow_test_bypass=value))
                self.assertEqual(store.get("book"), before)
            self.assertIsNone(self.atomic_confirm(store, allow_test_bypass=True))
            self.assertIsNone(self.atomic_confirm(store, test=True, payment_checkout={"unexpected": True}))
            self.assertEqual(store.get("book"), before)
            self.prepare(store, "real")
            real = store.get("real")
            self.assertIsNone(self.atomic_confirm(store, "real", test=True))
            self.assertEqual(store.get("real"), real)

    def test_atomic_invalid_plan_or_checkout_leaves_prepared_attempt_untouched(self):
        for store in self.stores:
            self.prepare(store)
            before = store.get("book")
            changed = prepared_plan()
            changed["artifact_id"] = "2" * 32
            changed["plan_id"] = pdf_plan_identity(changed)
            valid = checkout_snapshot("book", "1.99", pay_url="https://pay.example.invalid/original",
                                      created_at=datetime.now(timezone.utc))
            for overrides in ({"plan_id": "stale"}, {"confirmed_plan": confirmed_plan(changed)},
                              {"confirmed_plan": {}}, {"status": JobStatus.success},
                              *({"payment_checkout": value} for value in (
                                  None, {}, {**valid, "amount": "0.01"}, {**valid, "order_no": "other"},
                                  {**valid, "created_at": None}, {**valid, "channel": "unknown"}))):
                self.assertIsNone(self.atomic_confirm(store, **overrides))
                self.assertEqual(store.get("book"), before)
                self.assertEqual(len(store.list_dispatches("book")), 1)

    def test_atomic_confirmation_cannot_override_payment_review_or_existing_authority(self):
        for store in self.stores:
            cases = ({"payment_resolution": {"state": "paid_review"}},
                     {"payment_resolution": {"state": "external_refund_recorded"}},
                     {"payment_entitlement": {"state": "paid", "order_no": "case-2", "amount": "1.99"}},
                     {"payment_entitlement": {"state": "quoted", "order_no": "other", "amount": "1.99"}},
                     {"payment_entitlement": {"state": "test_authorized", "source": "browser", "order_no": "case-4", "amount": "1.99"}},
                     {"payment_resolution": {"state": "paid", "source": "verified_webhook", "amount": "1.99"}},
                     {"payment_resolution": {"state": "closed"}})
            for index, options in enumerate(cases):
                key = f"case-{index}"
                self.prepare(store, key, is_test_order=True, **options)
                before = store.get(key)
                self.assertIsNone(self.atomic_confirm(store, key, test=True))
                self.assertIsNone(self.atomic_confirm(store, key))
                self.assertEqual(store.get(key), before)

    def test_atomic_confirmation_does_not_replace_an_existing_first_checkout(self):
        for store in self.stores:
            self.prepare(store, is_test_order=True)
            saved = store.get("book")
            checkout = checkout_snapshot("book", "1.99", pay_url="https://pay.example.invalid/original",
                                         created_at=datetime.now(timezone.utc))
            store.update_status("book", JobStatus.awaiting_confirmation,
                                translation_stats={**saved.translation_stats, "payment_checkout": checkout})
            before = store.get("book")
            self.assertIsNone(self.atomic_confirm(store))
            self.assertIsNone(self.atomic_confirm(store, test=True))
            self.assertEqual(store.get("book"), before)
            self.assertEqual(len(store.list_dispatches("book")), 1)

    def test_atomic_confirmation_has_one_winner_across_independent_sql_connections(self):
        for store in self.stores:
            self.prepare(store, is_test_order=True)
            other = PersistentJobStore(self.engine) if store is self.sql else store
            barrier = threading.Barrier(8)
            def confirm(index):
                barrier.wait(timeout=5)
                return self.atomic_confirm(store if index % 2 else other, test=True)
            with ThreadPoolExecutor(max_workers=8) as pool:
                rows = list(pool.map(confirm, range(8)))
            self.assertEqual(sum(row is not None for row in rows), 1)
            saved = store.get("book")
            self.assertEqual(saved.status, JobStatus.pending)
            self.assertEqual(store.get("book"), other.get("book"))
            self.assertEqual(len(store.list_dispatches("book")), 2)

    def test_atomic_cancel_race_never_changes_a_cancelled_plan_or_adds_delivery_intent(self):
        for store in self.stores:
            for index in range(3):
                key = f"atomic-race-{index}"
                self.prepare(store, key)
                barrier = threading.Barrier(2)
                def confirm():
                    barrier.wait(timeout=5)
                    return self.atomic_confirm(store, key)
                def cancel():
                    barrier.wait(timeout=5)
                    return store.update_status(key, JobStatus.cancelled, "cancel",
                                               expected_statuses={JobStatus.awaiting_confirmation})
                with ThreadPoolExecutor(max_workers=2) as pool:
                    left, right = pool.submit(confirm), pool.submit(cancel)
                    confirmed, _ = left.result(timeout=5), right.result(timeout=5)
                saved = store.get(key)
                self.assertIn(saved.status, {JobStatus.cancelled, JobStatus.pending_payment})
                self.assertEqual(confirmed is not None, saved.status == JobStatus.pending_payment)
                self.assertIsNone(saved.output_path)
                self.assertEqual(len(store.list_dispatches(key)), 1)
                self.assertIsNone(self.atomic_confirm(store, key))

    def test_atomic_outbox_failure_rolls_back_new_attempt_entitlement_and_plan(self):
        for store in self.stores:
            self.prepare(store, is_test_order=True)
            before = store.get("book")
            def fail(*_args):
                raise RuntimeError("injected outbox failure")
            if store is self.memory:
                with patch.object(store, "_prepare_dispatch_locked", side_effect=fail):
                    with self.assertRaises(RuntimeError):
                        self.atomic_confirm(store, test=True)
            else:
                event.listen(DispatchRecord, "before_insert", fail)
                try:
                    with self.assertRaises(RuntimeError):
                        self.atomic_confirm(store, test=True)
                finally:
                    event.remove(DispatchRecord, "before_insert", fail)
            self.assertEqual(store.get("book"), before)
            self.assertEqual(len(store.list_dispatches("book")), 1)

    def test_atomic_sql_commit_then_raise_keeps_original_committed_checkout_on_retry(self):
        for index, test in enumerate((False, True)):
            key = f"uncertain-{index}"
            self.prepare(self.sql, key, is_test_order=test)
            def lost_ack(session):
                if session.bind is self.engine:
                    raise RuntimeError("lost acknowledgement after durable commit")
            event.listen(Session, "after_commit", lost_ack)
            try:
                with self.assertRaisesRegex(RuntimeError, "lost acknowledgement"):
                    self.atomic_confirm(self.sql, key, test=test)
            finally:
                event.remove(Session, "after_commit", lost_ack)
            reopened = PersistentJobStore(self.engine)
            saved = reopened.get(key)
            self.assertEqual(saved.status, JobStatus.pending if test else JobStatus.pending_payment)
            self.assertIsNone(self.atomic_confirm(reopened, key, test=test))
            self.assertEqual(reopened.get(key), saved)
            self.assertIsNone(reopened.rollback_pdf_confirmation(
                key, attempt_id=saved.translation_stats["attempt_id"], message="late failure"))
            self.assertEqual(len(reopened.list_dispatches(key)), 2 if test else 1)

    def test_preparation_finishes_owner_and_waits_for_confirmation_without_deliverable(self):
        for store in self.stores:
            self.start(store)
            saved = store.finish_pdf_preparation("book", "preparation", "owner", prepared_plan())
            self.assertEqual(saved.status, JobStatus.awaiting_confirmation)
            self.assertIsNone(saved.output_path)
            self.assertEqual(saved.expected_amount, "1.99")
            self.assertEqual(saved.translation_stats["preserved"], {"counter": 7})
            self.assertEqual(saved.translation_stats["pdf_conversion"]["phase"], "prepared")
            execution = store.get_execution("book", "preparation")
            self.assertEqual((execution["state"], execution["owner"]), ("finished", ""))
            self.assertFalse(store.heartbeat_execution("book", "preparation", "owner"))
            self.assertFalse(store.finish_execution("book", "preparation", "owner"))
            self.assertEqual(len(store.list_dispatches("book")), 1)  # Only original preparation.
            saved.translation_stats["pdf_conversion"]["report"]["page_count"] = 99
            self.assertEqual(store.get("book").translation_stats["pdf_conversion"]["report"]["page_count"], 1)

    def test_cancelled_failed_or_superseded_preparation_cannot_publish_metadata(self):
        for store in self.stores:
            for status in (JobStatus.cancelled, JobStatus.failed, JobStatus.success):
                key = status.value
                self.start(store, key)
                store.update_status(key, status)
                before = store.get(key)
                self.assertIsNone(store.finish_pdf_preparation(key, "preparation", "owner", prepared_plan()))
                self.assertEqual(store.get(key), before)
            self.start(store, "attempt")
            store.update_status("attempt", JobStatus.running, translation_stats={"attempt_id": "next"})
            self.assertIsNone(store.finish_pdf_preparation("attempt", "preparation", "owner", prepared_plan()))

    def test_preparation_rejects_wrong_owner_and_changed_source_or_quote(self):
        for store in self.stores:
            self.start(store)
            before = store.get("book")
            for attempt, owner, plan in (("wrong", "owner", prepared_plan()),
                                         ("preparation", "other", prepared_plan()),
                                         ("preparation", "", prepared_plan()),
                                         ("preparation", "owner", prepared_plan(source_sha256="2" * 64)),
                                         ("preparation", "owner", prepared_plan(source_bytes=124)),
                                         ("preparation", "owner", prepared_plan(amount="2.99")),
                                         ("preparation", "owner", {})):
                self.assertIsNone(store.finish_pdf_preparation("book", attempt, owner, plan))
                self.assertEqual(store.get("book"), before)

    def test_duplicate_preparation_and_concurrent_confirmation_have_single_winner(self):
        for store in self.stores:
            self.prepare(store)
            self.assertIsNone(store.finish_pdf_preparation("book", "preparation", "owner", prepared_plan()))
            plan = prepared_plan()
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: store.begin_pdf_confirmation(
                    "book", plan_id=plan["plan_id"], confirmed_plan=confirmed_plan(plan)), range(12)))
            winners = [row for row in results if row is not None]
            self.assertEqual(len(winners), 1)
            saved = store.get("book")
            self.assertEqual(saved.status, JobStatus.confirming)
            self.assertNotEqual(saved.translation_stats["attempt_id"], "preparation")
            self.assertEqual(len(saved.translation_stats["attempt_id"]), 32)
            self.assertEqual(saved.translation_stats["preserved"], {"counter": 7})

    def test_confirm_rejects_stale_plan_or_valid_but_different_immutable_artifact(self):
        for store in self.stores:
            self.prepare(store)
            before = store.get("book")
            changed = prepared_plan()
            changed["artifact_id"] = "2" * 32
            changed["plan_id"] = pdf_plan_identity(changed)
            self.assertIsNone(store.begin_pdf_confirmation("book", plan_id=prepared_plan()["plan_id"],
                                                           confirmed_plan=confirmed_plan(changed)))
            self.assertIsNone(store.begin_pdf_confirmation("book", plan_id="stale", confirmed_plan=confirmed_plan(prepared_plan())))
            self.assertEqual(store.get("book"), before)

    def test_concurrent_cancel_and_confirm_serialize_on_the_same_parent(self):
        for store in self.stores:
            for index in range(4):
                key = f"race-{index}"
                self.prepare(store, key)
                barrier = threading.Barrier(2)
                def claim():
                    barrier.wait(timeout=5)
                    return self.claim(store, key)
                def cancel():
                    barrier.wait(timeout=5)
                    return store.update_status(key, JobStatus.cancelled, "cancel",
                                               expected_statuses={JobStatus.awaiting_confirmation})
                with ThreadPoolExecutor(max_workers=2) as pool:
                    claim_future, cancel_future = pool.submit(claim), pool.submit(cancel)
                    claimed, _ = claim_future.result(timeout=5), cancel_future.result(timeout=5)
                saved = store.get(key)
                self.assertIn(saved.status, {JobStatus.cancelled, JobStatus.confirming})
                self.assertEqual(claimed is not None, saved.status == JobStatus.confirming)
                self.assertIsNone(saved.output_path)
                self.assertEqual(len(store.list_dispatches(key)), 1)

    def test_independent_sql_store_instances_share_confirmation_claim(self):
        self.prepare(self.sql)
        other = PersistentJobStore(self.engine)
        barrier = threading.Barrier(2)
        def claim(store):
            barrier.wait(timeout=5)
            return self.claim(store)
        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = pool.submit(claim, self.sql), pool.submit(claim, other)
            winners = [row for row in (left.result(timeout=5), right.result(timeout=5)) if row is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.sql.get("book"), other.get("book"))

    def test_legacy_or_partial_pdf_preparation_metadata_is_not_promoted(self):
        for store in self.stores:
            for index, stale in enumerate(({}, {"product": "pdf_text_conversion", "phase": "preparing"},
                                          {**new_pdf_plan("a" * 64, 123, "1.99"), "schema_version": 0})):
                key = f"old-{index}"
                self.start(store, key)
                store.update_status(key, JobStatus.running, translation_stats={"pdf_conversion": stale})
                before = store.get(key)
                self.assertIsNone(store.finish_pdf_preparation(key, "preparation", "owner", prepared_plan()))
                self.assertEqual(store.get(key), before)

    def test_rollback_is_attempt_fenced_and_a_new_confirmation_uses_new_delivery_attempt(self):
        for store in self.stores:
            self.prepare(store)
            first = self.claim(store).translation_stats["attempt_id"]
            self.assertIsNone(store.rollback_pdf_confirmation("book", attempt_id="wrong", message="old"))
            rolled = store.rollback_pdf_confirmation("book", attempt_id=first, message="gateway unavailable")
            self.assertEqual(rolled.status, JobStatus.awaiting_confirmation)
            self.assertEqual(rolled.translation_stats["pdf_conversion"], prepared_plan())
            second = self.claim(store).translation_stats["attempt_id"]
            self.assertNotEqual(first, second)
            before = store.get("book")
            self.assertIsNone(store.rollback_pdf_confirmation("book", attempt_id=first, message="late rollback"))
            self.assertIsNone(store.finish_pdf_confirmation("book", attempt_id=first, status=JobStatus.pending_payment,
                                                           message="late success"))
            self.assertEqual(store.get("book"), before)

    def test_paid_checkout_snapshot_and_original_quote_survive_sql_reopen(self):
        for store in self.stores:
            self.prepare(store)
            claimed = self.claim(store)
            checkout = checkout_snapshot("book", "1.99", pay_url="https://pay.example.invalid/original",
                                         created_at=datetime.now(timezone.utc))
            saved = store.finish_pdf_confirmation("book", attempt_id=claimed.translation_stats["attempt_id"],
                                                 status=JobStatus.pending_payment, message="pay", payment_checkout=checkout)
            self.assertEqual(saved.status, JobStatus.pending_payment)
            self.assertEqual(saved.expected_amount, "1.99")
            self.assertEqual(saved.translation_stats["payment_checkout"], checkout)
            self.assertIsNone(saved.output_path)
            self.assertEqual(len(store.list_dispatches("book")), 1)
            self.assertIsNone(store.finish_pdf_confirmation("book", attempt_id=claimed.translation_stats["attempt_id"],
                                                           status=JobStatus.pending_payment, message="duplicate", payment_checkout=checkout))
        self.assertEqual(PersistentJobStore(self.engine).get("book"), self.sql.get("book"))

    def test_test_order_flag_alone_never_authorizes_free_delivery(self):
        for store in self.stores:
            self.prepare(store, is_test_order=True)
            attempt = self.claim(store).translation_stats["attempt_id"]
            self.assertIsNone(store.finish_pdf_confirmation("book", attempt_id=attempt, status=JobStatus.pending, message="free"))
            grant_test_entitlement(store, store.get("book"))
            saved = store.finish_pdf_confirmation("book", attempt_id=attempt, status=JobStatus.pending, message="test bypass")
            self.assertEqual(saved.status, JobStatus.pending)
            self.assertEqual([r["attempt_id"] for r in store.list_dispatches("book")].count(attempt), 1)
            self.assertIsNone(store.finish_pdf_confirmation("book", attempt_id=attempt, status=JobStatus.pending, message="repeat"))
            self.assertEqual([r["attempt_id"] for r in store.list_dispatches("book")].count(attempt), 1)

    def test_invalid_or_foreign_test_entitlement_does_not_issue_outbox(self):
        for store in self.stores:
            for index, overrides in enumerate(({"state": "paid"}, {"source": "browser"},
                                              {"order_no": "other"}, {"amount": "0.01"})):
                key = f"case-{index}"
                self.prepare(store, key, is_test_order=True)
                attempt = self.claim(store, key).translation_stats["attempt_id"]
                entitlement = {"state": "test_authorized", "source": "server_test_bypass", "order_no": key,
                               "amount": "1.99", **overrides}
                store.save_payment_entitlement(key, entitlement, expected={})
                self.assertIsNone(store.finish_pdf_confirmation(key, attempt_id=attempt, status=JobStatus.pending, message="free"))
                self.assertEqual(len(store.list_dispatches(key)), 1)

    def test_cancel_after_claim_blocks_finish_and_stale_rollback(self):
        for store in self.stores:
            self.prepare(store)
            attempt = self.claim(store).translation_stats["attempt_id"]
            store.update_status("book", JobStatus.cancelled, "cancel")
            before = store.get("book")
            self.assertIsNone(store.finish_pdf_confirmation("book", attempt_id=attempt, status=JobStatus.pending_payment, message="pay"))
            self.assertIsNone(store.rollback_pdf_confirmation("book", attempt_id=attempt, message="undo cancel"))
            self.assertEqual(store.get("book"), before)

    def test_recovered_preparation_same_attempt_new_owner_fences_old_worker(self):
        for store in self.stores:
            self.start(store)
            self.assertEqual(store.recover_execution("book", "preparation", "owner",
                             stale_before=datetime.now(timezone.utc), max_recoveries=2), "recovered")
            self.assertTrue(store.begin_execution("book", "preparation", "replacement"))
            self.assertIsNone(store.finish_pdf_preparation("book", "preparation", "owner", prepared_plan()))
            self.assertIsNotNone(store.finish_pdf_preparation("book", "preparation", "replacement", prepared_plan()))

    def test_ordinary_or_unsupported_pdf_product_cannot_use_preparation_transition(self):
        for store in self.stores:
            for index, options in enumerate(({"output_mode": OutputMode.simplified}, {"enable_translation": True},
                                            {"enable_precision_polish": True}, {"bilingual": True}, {"batch_id": "batch"})):
                key = f"unsupported-{index}"
                self.start(store, key, **options)
                before = store.get(key)
                self.assertIsNone(store.finish_pdf_preparation(key, "preparation", "owner", prepared_plan()))
                self.assertEqual(store.get(key), before)

    def test_finish_rejects_incomplete_or_foreign_checkout_without_partial_state(self):
        for store in self.stores:
            self.prepare(store)
            attempt = self.claim(store).translation_stats["attempt_id"]
            valid = checkout_snapshot("book", "1.99", pay_url="https://pay.example.invalid/original",
                                      created_at=datetime.now(timezone.utc))
            before = store.get("book")
            for checkout in (None, {}, {**valid, "order_no": "other"}, {**valid, "amount": "0.01"},
                             {**valid, "created_at": None}, {**valid, "channel": "unknown"}):
                self.assertIsNone(store.finish_pdf_confirmation("book", attempt_id=attempt,
                                  status=JobStatus.pending_payment, message="pay", payment_checkout=checkout))
                self.assertEqual(store.get("book"), before)

    def test_memory_outbox_failure_does_not_partially_finish_confirmation(self):
        self.prepare(self.memory, is_test_order=True)
        attempt = self.claim(self.memory).translation_stats["attempt_id"]
        grant_test_entitlement(self.memory, self.memory.get("book"))
        before = self.memory.get("book")
        with patch.object(self.memory, "_prepare_dispatch_locked", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.memory.finish_pdf_confirmation("book", attempt_id=attempt, status=JobStatus.pending, message="test")
        self.assertEqual(self.memory.get("book"), before)
        self.assertEqual(len(self.memory.list_dispatches("book")), 1)

    def test_sql_outbox_failure_rolls_back_job_plan_and_checkout(self):
        self.prepare(self.sql, is_test_order=True)
        attempt = self.claim(self.sql).translation_stats["attempt_id"]
        grant_test_entitlement(self.sql, self.sql.get("book"))
        before = self.sql.get("book")
        def fail(*_args):
            raise RuntimeError("injected")
        event.listen(DispatchRecord, "before_insert", fail)
        try:
            with self.assertRaises(RuntimeError):
                self.sql.finish_pdf_confirmation("book", attempt_id=attempt, status=JobStatus.pending, message="test")
        finally:
            event.remove(DispatchRecord, "before_insert", fail)
        self.assertEqual(self.sql.get("book"), before)
        self.assertEqual(len(self.sql.list_dispatches("book")), 1)

    def test_sql_preparation_failure_rolls_back_plan_and_execution_together(self):
        self.start(self.sql)
        before, execution = self.sql.get("book"), self.sql.get_execution("book", "preparation")
        def fail(*_args):
            raise RuntimeError("injected")
        event.listen(ExecutionRecord, "before_update", fail)
        try:
            with self.assertRaises(RuntimeError):
                self.sql.finish_pdf_preparation("book", "preparation", "owner", prepared_plan())
        finally:
            event.remove(ExecutionRecord, "before_update", fail)
        self.assertEqual(self.sql.get("book"), before)
        self.assertEqual(self.sql.get_execution("book", "preparation"), execution)


if __name__ == "__main__":
    unittest.main(verbosity=2)
