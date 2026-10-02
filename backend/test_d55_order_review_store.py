"""Manual review transactions against real isolated SQLite; no external IO."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import create_engine, event, select, update

with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv", return_value=False):
    from app.admin.reviews import OrderReviewError, OrderReviewService
    from app.storage_db import (Base, PersistentJobStore, JobRecord, ChapterRecord, ChunkRecord, StageRecord,
        OrderReviewRecord, OrderReviewEventRecord)
    from app.models import Job, JobStatus, OutputMode
    from app.domain.payment_entitlement import (quote_entitlement, restart_entitlement_reason,
        grant_verified_entitlement, precision_polish_entitlement_reason)


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ((socket.socket, "connect"), (socket, "create_connection"), (socket, "getaddrinfo")):
            self.stack.enter_context(patch.object(*target, side_effect=AssertionError("Network forbidden")))
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.engine = create_engine(f"sqlite:///{self.root / 'orders.db'}",
            connect_args={"check_same_thread": False, "timeout": 15})
        self.stack.callback(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.cost = {"coverage": "complete", "actual_cost_cny": "0.23", "calls": 1}
        self.service = OrderReviewService(self.store, self.uploads,
                                         cost_provider=lambda job: copy.deepcopy(self.cost))

    def job(self, key="one", **values):
        source = self.uploads / f"{key}.epub"
        source.write_bytes(b"isolated original file for transaction tests")
        artifact = self.root / f"{key}-prior.epub"
        artifact.write_bytes(b"prior successful immutable artifact")
        defaults = dict(id=key, source_filename=f"{key}.epub", input_path=str(source),
            output_mode=OutputMode.simplified, trace_id="isolated", expected_amount="5.99",
            status=JobStatus.cancelled, error_code="PAYMENT_REVIEW_REQUIRED", output_path=str(artifact),
            payment_resolution={"state": "paid_review", "source": "verified_query", "amount": "5.99",
                                "original_cancel_message": "owner cancelled"},
            translation_stats={"attempt_id": "old-attempt", "prompt_tokens": 123,
                "payment_checkout": {"schema_version": 1, "order_no": key, "amount": "5.99", "channel": "page"},
                "translation_pricing": {"schema_version": 2, "frozen": "unchanged"}})
        defaults.update(values)
        job = Job(**defaults)
        if job.enable_translation or job.enable_precision_polish:
            job.payment_entitlement = quote_entitlement(job)
            job.payment_entitlement.update(state="paid", source="verified_query")
        self.store.add(job)
        return job

    def params(self, key="one", action="note", **values):
        view = self.service.snapshot(key)
        result = dict(job_id=key, action=action, request_id=str(uuid.uuid4()),
            expected_revision=view["revision"], expected_context=view["context"],
            actor="admin-test", note="private-note-canary", evidence="private-evidence-canary",
            refund_reference="external-full-refund-verified" if action == "record_external_refund" else "",
            acknowledge_cost=True, trade={"out_trade_no": view["order_no"], "total_amount": "5.99",
                                        "trade_no": "verified-trade", "trade_status": "TRADE_SUCCESS"})
        result.update(values)
        return result

    def counts(self):
        with self.store._Session() as session:
            return (len(session.scalars(select(OrderReviewRecord)).all()),
                    len(session.scalars(select(OrderReviewEventRecord)).all()),
                    len(self.store.list_dispatches()))

    def assert_rejected(self, params, code=None):
        with self.assertRaises(OrderReviewError) as caught:
            self.service.apply(**params)
        if code:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_snapshot_read_only_stable_and_no_direct_close_for_paid_review(self):
        original = self.job()
        first = self.service.snapshot("one")
        self.assertEqual(first, self.service.snapshot("one"))
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertTrue(first["needs_attention"])
        self.assertIn("fulfill", first["allowed_actions"])
        self.assertNotIn("close_review", first["allowed_actions"])
        self.assert_rejected(self.params(action="close_review"), "action_unavailable")
        self.assertEqual(self.store.get("one").payment_resolution, original.payment_resolution)

    def test_note_is_private_append_only_and_does_not_change_order(self):
        original = self.job()
        params = self.params()
        result = self.service.apply(**params)
        self.assertEqual(result["review"]["revision"], 1)
        self.assertEqual(self.counts(), (1, 1, 0))
        current = self.store.get("one")
        for key in ("status", "expected_amount", "payment_entitlement", "payment_resolution", "translation_stats", "output_path"):
            self.assertEqual(getattr(current, key), getattr(original, key))
        self.assertNotIn(params["note"], json.dumps(asdict(current), default=str))
        history = self.service.history("one")["items"]
        self.assertEqual(history[0]["note"], params["note"])
        self.assertNotIn(str(self.root), json.dumps(history))
        self.assertEqual(history[0]["result"]["prior_artifact_job_ids"], ["one"])

    def test_fulfill_preserves_original_plan_channel_price_and_old_artifact(self):
        original = self.job(enable_translation=True, translation_quality="literary", cache_policy="fresh",
            translation_strategy="academic", glossary={"term": "术语"})
        artifact_hash = hashlib.sha256(Path(original.output_path).read_bytes()).hexdigest()
        result = self.service.apply(**self.params(action="fulfill"))
        current = self.store.get("one")
        self.assertEqual(result["released"], ["one"])
        self.assertEqual(current.status, JobStatus.pending)
        self.assertIsNone(current.output_path)
        self.assertNotEqual(current.translation_stats["attempt_id"], "old-attempt")
        for key in ("expected_amount", "payment_entitlement", "translation_model", "translation_quality", "cache_policy", "translation_strategy", "glossary"):
            self.assertEqual(getattr(current, key), getattr(original, key))
        for key in ("payment_checkout", "translation_pricing"):
            self.assertEqual(current.translation_stats[key], original.translation_stats[key])
        self.assertEqual(current.translation_stats["cost_history"][0]["prompt_tokens"], 123)
        self.assertEqual(self.store.list_dispatches()[0]["attempt_id"], current.translation_stats["attempt_id"])
        self.assertEqual(hashlib.sha256(Path(original.output_path).read_bytes()).hexdigest(), artifact_hash)

    def test_identical_replay_after_completion_never_republishes(self):
        self.job()
        params = self.params(action="fulfill")
        self.service.apply(**params)
        attempt = self.store.get("one").translation_stats["attempt_id"]
        with self.engine.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.id == "one").values(
                status="success", output_path="existing-output.epub"))
        self.assertEqual(self.store.get("one").status, JobStatus.success)
        self.service = OrderReviewService(PersistentJobStore(self.engine), self.uploads,
                                         cost_provider=lambda job: self.cost)
        duplicate = self.service.apply(**{**params, "trade": None})
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["released"], [])
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(self.store.get("one").translation_stats["attempt_id"], attempt)
        self.assert_rejected({**params, "note": "different"}, "idempotency_conflict")

    def test_concurrent_duplicate_request_only_one_attempt_and_event(self):
        self.job()
        params = self.params(action="fulfill")
        barrier = threading.Barrier(6)
        def run(_):
            barrier.wait()
            return self.service.apply(**params)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(run, range(6)))
        self.assertEqual(sum(not result["duplicate"] for result in results), 1)
        self.assertEqual(sum(len(result["released"]) for result in results), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concurrent_fulfill_refund_cas_allows_only_one_disposition(self):
        self.job()
        params = [self.params(action=action) for action in ("fulfill", "record_external_refund")]
        barrier = threading.Barrier(2)
        def run(params):
            barrier.wait()
            try: return self.service.apply(**params)
            except OrderReviewError as error: return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, params))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn("context_changed", results)
        self.assertEqual(self.counts()[:2], (1, 1))
        current = self.store.get("one")
        self.assertEqual(len(self.store.list_dispatches()), int(current.status == JobStatus.pending))

    def test_fulfill_requires_exact_fresh_payment_and_cost_ack(self):
        self.job()
        params = self.params(action="fulfill")
        for change in ({"trade": None}, {"trade": {}}, {"acknowledge_cost": False},
                       *({"trade": {**params["trade"], key: value}} for key, value in
                         (("out_trade_no", "other"), ("total_amount", "0"), ("total_amount", "6.99"),
                          ("trade_no", ""), ("trade_status", "WAIT_BUYER_PAY")))):
            self.assert_rejected({**params, **change})
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(self.store.get("one").status, JobStatus.cancelled)

    def test_source_missing_symlink_and_outside_are_not_fulfillable(self):
        for index, kind in enumerate(("missing", "symlink", "outside", "empty")):
            key = f"source{index}"
            job = self.job(key)
            source = Path(job.input_path)
            if kind == "missing": source.unlink()
            elif kind == "symlink":
                source.unlink(); source.symlink_to(Path(job.output_path))
            elif kind == "empty": source.write_bytes(b"")
            else:
                with self.engine.begin() as connection:
                    connection.execute(update(JobRecord).where(JobRecord.id == key).values(input_path=job.output_path))
            view = self.service.snapshot(key)
            self.assertNotIn("fulfill", view["allowed_actions"])
            self.assertIn("source_unavailable", [reason["code"] for reason in view["reasons"]])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_upload_root_alias_is_accepted_but_inner_alias_is_rejected(self):
        job = self.job()
        alias = self.root / "root-alias"
        alias.symlink_to(self.uploads, target_is_directory=True)
        with self.engine.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.id == "one").values(input_path=str(alias / "one.epub")))
        service = OrderReviewService(self.store, alias, cost_provider=lambda job: self.cost)
        self.assertIn("fulfill", service.snapshot("one")["allowed_actions"])
        inside = self.uploads / "inside"
        inside.symlink_to(self.uploads, target_is_directory=True)
        with self.engine.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.id == "one").values(input_path=str(inside / "one.epub")))
        self.assertNotIn("fulfill", service.snapshot("one")["allowed_actions"])

    def test_missing_frozen_ai_purchase_is_never_reconstructed(self):
        self.job(enable_translation=True)
        with self.engine.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.id == "one").values(payment_entitlement_json="{}"))
        view = self.service.snapshot("one")
        self.assertNotIn("fulfill", view["allowed_actions"])
        self.assertIn("purchase_plan_unknown", [reason["code"] for reason in view["reasons"]])
        self.assert_rejected(self.params(action="fulfill"), "action_unavailable")

    def test_refund_private_evidence_public_safe_marker_and_all_retries_blocked(self):
        for key, ai in (("plain", False), ("ai", True)):
            original = self.job(key, enable_translation=ai)
            self.service.apply(**self.params(key, "record_external_refund"))
            current = self.store.get(key)
            self.assertEqual(current.status, JobStatus.cancelled)
            self.assertIsNone(current.error_code)
            self.assertEqual(current.expected_amount, original.expected_amount)
            self.assertEqual(current.payment_entitlement, original.payment_entitlement)
            self.assertEqual(current.output_path, original.output_path)
            public = json.dumps(current.payment_resolution)
            self.assertNotIn("private-", public)
            self.assertNotIn("external-full", public)
            self.assertEqual(restart_entitlement_reason(current), "refund_recorded")
            restarted, reason = self.store.restart_translation_attempt(key, attempt_id="must-not-execute",
                action_label="retry", max_free_retries=-1, started_at=datetime.now(timezone.utc))
            self.assertEqual(reason, "refund_recorded")
            self.assertEqual(restarted.status, JobStatus.cancelled)
            self.assertEqual(self.store.settle_verified_payment(key, source="verified_query", amount="5.99")["unchanged"], [key])
            self.assertEqual(grant_verified_entitlement(self.store, current, "5.99", "verified_query"), current.payment_entitlement if ai else {})
        self.assertEqual(self.store.list_dispatches(), [])

    def test_unresolved_paid_review_blocks_normal_conversion_retry(self):
        self.job()
        current, reason = self.store.restart_translation_attempt("one", attempt_id="blocked",
            action_label="retry", max_free_retries=-1, started_at=datetime.now(timezone.utc))
        self.assertEqual(reason, "payment_review_required")
        self.assertEqual(current.status, JobStatus.cancelled)

    def test_refund_requires_evidence_and_reference(self):
        self.job()
        params = self.params(action="record_external_refund")
        for name in ("note", "evidence", "refund_reference"):
            self.assert_rejected({**params, name: ""}, "invalid_input")
        self.assertEqual(self.counts(), (0, 0, 0))

    def batch(self):
        return [self.job(f"batch{index}", batch_id="group", batch_index=index, batch_size=3,
                         expected_amount="5.99" if index == 0 else "") for index in range(3)]

    def test_batch_child_addresses_full_order_and_full_amount_once(self):
        originals = self.batch()
        params = self.params("batch2", "fulfill")
        self.assertEqual(params["trade"]["out_trade_no"], "batch_group")
        result = self.service.apply(**params)
        self.assertEqual(result["released"], [job.id for job in originals])
        self.assertEqual(result["review"]["scope_count"], 3)
        self.assertEqual(self.counts(), (1, 1, 3))
        attempts = {self.store.get(job.id).translation_stats["attempt_id"] for job in originals}
        self.assertEqual(len(attempts), 3)
        self.assertEqual([self.store.get(job.id).expected_amount for job in originals], ["5.99", "", ""])

    def test_mixed_batch_never_partially_fulfills_or_refunds_or_closes(self):
        self.batch()
        with self.engine.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.id == "batch1").values(status="success"))
        view = self.service.snapshot("batch0")
        self.assertEqual(view["allowed_actions"], ["note"])
        for action in ("fulfill", "record_external_refund", "close_review"):
            self.assert_rejected(self.params("batch0", action), "action_unavailable")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_malformed_batch_snapshot_explicitly_blocked_and_actions_strict(self):
        self.batch()
        for value in ("4", "malformed"):
            with self.engine.begin() as connection:
                connection.execute(update(JobRecord).where(JobRecord.id == "batch1").values(batch_size=value))
            view = self.service.snapshot("batch0")
            self.assertEqual(view["allowed_actions"], [])
            self.assertEqual(view["reasons"][0]["code"], "invalid_batch")
            self.assert_rejected(self.params("batch0"), "invalid_batch")
            with self.assertRaises(OrderReviewError): self.service.history("batch0")

    def test_invalid_peer_metadata_blocks_whole_scope_without_read_failure_or_mutation(self):
        originals = self.batch()
        file_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                      for job in originals for path in (Path(job.input_path), Path(job.output_path))}
        with self.engine.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.id == "batch1").values(status="malformed"))
        with self.engine.connect() as connection:
            before = list(connection.execute(select(JobRecord).order_by(JobRecord.id)).mappings())
        for key in ("batch0", "batch1", "batch2"):
            view = self.service.snapshot(key)
            self.assertEqual(view["order_no"], "batch_group")
            self.assertEqual(view["scope_count"], 3)
            self.assertEqual(view["allowed_actions"], [])
            self.assertTrue(view["needs_attention"])
            self.assertEqual(view["reasons"][0]["code"], "invalid_metadata")
            for action in ("note", "fulfill", "record_external_refund", "close_review"):
                self.assert_rejected(self.params(key, action), "invalid_metadata")
        with self.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(JobRecord).order_by(JobRecord.id)).mappings()), before)
        self.assertEqual(self.counts(), (0, 0, 0))
        for name, digest in file_hashes.items():
            self.assertEqual(hashlib.sha256(Path(name).read_bytes()).hexdigest(), digest)

    def test_invalid_single_order_metadata_is_nonactionable_diagnostic(self):
        self.job()
        with self.engine.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.id == "one").values(output_mode="malformed"))
        view = self.service.snapshot("one")
        self.assertEqual(view["order_no"], "one")
        self.assertEqual(view["scope_count"], 1)
        self.assertEqual(view["allowed_actions"], [])
        self.assertEqual(view["reasons"][0]["code"], "invalid_metadata")
        self.assert_rejected(self.params(), "invalid_metadata")

    def test_mapping_type_and_overflow_errors_are_not_silently_coerced(self):
        self.job()
        for kind in (TypeError, OverflowError):
            with patch("app.admin.reviews._record_to_job", side_effect=kind("private bad metadata")):
                view = self.service.snapshot("one")
                self.assertEqual(view["allowed_actions"], [])
                self.assertEqual(view["reasons"][0]["code"], "invalid_metadata")
                error = self.assert_rejected(self.params(), "invalid_metadata")
                self.assertNotIn("private bad", str(error))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_audit_or_outbox_insert_failure_rolls_back_all_mutations(self):
        for index, table in enumerate(("admin_order_review_events", "job_dispatch_outbox")):
            key = f"rollback{index}"
            original = self.job(key)
            params = self.params(key, "fulfill")
            def fail(connection, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("INSERT INTO " + table.upper()):
                    raise RuntimeError("injected transaction failure")
            event.listen(self.engine, "before_cursor_execute", fail)
            try:
                with self.assertRaises(RuntimeError): self.service.apply(**params)
            finally: event.remove(self.engine, "before_cursor_execute", fail)
            current = self.store.get(key)
            self.assertEqual(current.status, original.status)
            self.assertEqual(current.translation_stats, original.translation_stats)
            self.assertEqual(current.payment_resolution, original.payment_resolution)
            self.assertEqual(current.output_path, original.output_path)
            self.assertTrue(Path(original.output_path).exists())
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_batch_second_dispatch_failure_rolls_back_all_members(self):
        originals = self.batch()
        real = self.store._ensure_dispatch_in_session
        calls = []
        def fail(session, row, **kwargs):
            calls.append(row.id)
            if len(calls) == 2: raise RuntimeError("second member failure")
            return real(session, row, **kwargs)
        with patch.object(self.store, "_ensure_dispatch_in_session", side_effect=fail):
            with self.assertRaises(RuntimeError): self.service.apply(**self.params("batch0", "fulfill"))
        self.assertEqual(self.counts(), (0, 0, 0))
        for original in originals:
            self.assertEqual(self.store.get(original.id).translation_stats, original.translation_stats)
            self.assertEqual(self.store.get(original.id).status, JobStatus.cancelled)

    def test_case_close_cannot_change_financial_facts_and_reopens_on_new_facts(self):
        original = self.job(status=JobStatus.pending_payment, payment_resolution={}, translation_stats={})
        params = self.params(action="close_review")
        self.assertIn("close_review", self.service.snapshot("one")["allowed_actions"])
        result = self.service.apply(**params)
        self.assertFalse(result["review"]["needs_attention"])
        self.assertEqual(self.store.get("one").translation_stats, {})
        self.assertEqual(self.store.get("one").status, original.status)
        self.service.apply(**self.params(action="note"))
        self.assertEqual(self.service.snapshot("one")["state"], "closed")
        self.cost["calls"] += 1
        self.assertTrue(self.service.snapshot("one")["needs_attention"])
        self.assertEqual(self.service.snapshot("one")["state"], "open")
        self.assertEqual(self.counts(), (1, 2, 0))

    def test_stale_order_source_cost_or_case_version_rejected(self):
        original = self.job()
        params = self.params()
        Path(original.input_path).write_bytes(b"different source")
        self.assert_rejected(params, "context_changed")
        params = self.params()
        self.cost["calls"] += 1
        self.assert_rejected(params, "context_changed")
        params = self.params()
        self.service.apply(**params)
        self.assert_rejected({**params, "request_id": str(uuid.uuid4())}, "context_changed")

    def test_history_pagination_stable_on_append_and_bound_to_order(self):
        self.job(); self.job("two")
        for _ in range(4): self.service.apply(**self.params())
        first = self.service.history("one", limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.service.apply(**self.params())
        second = self.service.history("one", first["next_cursor"], limit=2)
        self.assertEqual(len(second["items"]), 2)
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
        self.assertIsNone(second["next_cursor"])
        for before in ("invalid", first["next_cursor"]):
            with self.assertRaises(OrderReviewError): self.service.history("two", before)
        for limit in (0, 101, True):
            with self.assertRaises(OrderReviewError): self.service.history("one", limit=limit)

    def test_cost_provider_unavailable_is_fail_closed(self):
        self.job()
        params = self.params(action="fulfill")
        with patch.object(self.service, "cost_provider", side_effect=RuntimeError("private ledger detail")):
            error = self.assert_rejected(params, "cost_unavailable")
            self.assertNotIn("private ledger", str(error))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_precision_polish_frozen_purchase_survives_manual_fulfillment(self):
        original = self.job(enable_precision_polish=True, polish_char_count=1000,
            translation_stats={"attempt_id": "old", "precision_polish": {
                "version": 1, "order_no": "one", "char_count": 1000, "quoted_amount": "1.99",
                "status": "failed", "refund_required": True}})
        self.service.apply(**self.params(action="fulfill"))
        current = self.store.get("one")
        self.assertEqual(current.payment_entitlement, original.payment_entitlement)
        self.assertEqual(precision_polish_entitlement_reason(current), "")
        self.assertEqual(current.translation_stats["precision_polish"]["status"], "pending")
        self.assertFalse(current.translation_stats["precision_polish"]["refund_required"])

    def progress(self):
        now = datetime.now(timezone.utc)
        with self.store._Session() as session:
            session.add(ChapterRecord(id="one:chapter", job_id="one", chapter_id="chapter",
                                      file_path="text/chapter.xhtml", status="completed"))
            session.add(ChunkRecord(id="one:chunk", job_id="one", chapter_id="chapter", chunk_id="chunk",
                locator="p:1", source_hash="oldhash", translated_text="old translation", status="translated",
                created_at=now, updated_at=now))
            session.add(StageRecord(id="one:stage", job_id="one", stage_name="old-attempt",
                                     started_at=now, status="completed"))
            session.commit()

    def test_fulfill_clears_current_progress_but_preserves_stage_history(self):
        self.job(enable_translation=True)
        self.progress()
        self.service.apply(**self.params(action="fulfill"))
        with self.store._Session() as session:
            self.assertIsNone(session.get(ChapterRecord, "one:chapter"))
            self.assertIsNone(session.get(ChunkRecord, "one:chunk"))
            self.assertIsNotNone(session.get(StageRecord, "one:stage"))

    def test_progress_deletion_rolls_back_with_audit_failure(self):
        self.job(enable_translation=True)
        self.progress()
        def fail(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO admin_order_review_events"):
                raise RuntimeError("injected audit failure")
        event.listen(self.engine, "before_cursor_execute", fail)
        try:
            with self.assertRaises(RuntimeError): self.service.apply(**self.params(action="fulfill"))
        finally: event.remove(self.engine, "before_cursor_execute", fail)
        with self.store._Session() as session:
            self.assertEqual(session.get(ChapterRecord, "one:chapter").status, "completed")
            self.assertEqual(session.get(ChunkRecord, "one:chunk").translated_text, "old translation")
            self.assertIsNotNone(session.get(StageRecord, "one:stage"))
        self.assertEqual(self.counts(), (0, 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
