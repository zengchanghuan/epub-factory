"""R10 local-volume transactions: real process/thread contention, no services."""
from contextlib import ExitStack
import copy
import json
import multiprocessing
import os
from pathlib import Path
import queue
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from app.domain.repair_repository import RepairMetadataError, RepairRepository


JOB_ID = "1" * 32
OTHER_ID = "2" * 32


def increment_worker(root, ready, start, results, count):
    try:
        repository = RepairRepository(root)
        ready.put(True)
        if not start.wait(10):
            raise RuntimeError("No start signal")
        for _ in range(count):
            with repository.transaction(JOB_ID) as job:
                job["count"] += 1
        results.put(None)
    except BaseException as exc:
        results.put(repr(exc))


def lock_worker(root, ready, release):
    with RepairRepository(root).lock("execution-slot-0") as held:
        ready.put(held)
        if not release.wait(10):
            raise RuntimeError("No release signal")


def budget_worker(root, ready, start, results):
    try:
        repository = RepairRepository(root)
        ready.put(True)
        if not start.wait(10):
            raise RuntimeError("No start signal")
        results.put(repository.reserve_gateway(100))
    except BaseException as exc:
        results.put((repr(exc), repr(exc.__cause__)))


class RepairRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fixepub-r10-repository-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repair"
        self.repository = RepairRepository(self.root)

    def create(self, **extra):
        saved = {"status": "pending_payment", "filename": "book.epub", "count": 0,
                 "expected_amount": "0.01", "quoted_amount": "5.99",
                 "out_trade_no": "repair_" + JOB_ID, "is_test_order": True,
                 "report": {"issues": [{"code": "TEST", "fixed": False}]},
                 "receipt_sent": False}
        saved.update(extra)
        with self.repository.transaction(JOB_ID, create=True) as job:
            job.update(saved)
        return copy.deepcopy(saved)

    def metadata(self):
        return self.root / JOB_ID / "order.json"

    def raw_metadata(self, value):
        directory = self.root / JOB_ID
        directory.mkdir(exist_ok=True)
        self.metadata().write_text(value, encoding="utf-8")

    def test_create_reopen_preserves_old_fields_and_amounts(self):
        expected = self.create(legacy_custom={"anything": [None, 3, "保留"]})
        self.assertEqual(RepairRepository(self.root).get(JOB_ID), expected)
        self.assertEqual(stat.S_IMODE(self.metadata().stat().st_mode), 0o600)
        self.assertEqual(self.repository.root, self.root.resolve())

    def test_get_is_detached_and_independent_instances_read_fresh(self):
        expected = self.create()
        stale = self.repository.get(JOB_ID)
        stale["report"]["issues"][0]["fixed"] = True
        self.assertEqual(self.repository.get(JOB_ID), expected)
        second = RepairRepository(self.root)
        with second.transaction(JOB_ID) as job:
            job.update(status="paid", payment_source="verified_webhook")
        stale.update(status="pending_payment", expected_amount="999")
        with self.repository.transaction(JOB_ID) as fresh:
            self.assertEqual(fresh["status"], "paid")
            fresh["pay_url"] = "https://example.invalid/payment"
        current = second.get(JOB_ID)
        self.assertEqual(current["status"], "paid")
        self.assertEqual(current["expected_amount"], "0.01")
        self.assertEqual(current["payment_source"], "verified_webhook")

    def test_missing_read_and_noncreating_transaction_do_not_create_order(self):
        self.assertIsNone(self.repository.get(JOB_ID))
        with self.repository.transaction(JOB_ID) as job:
            self.assertIsNone(job)
        self.assertFalse((self.root / JOB_ID).exists())
        self.assertFalse(list(self.root.glob("*/order.json")))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_one_hundred_unknown_reads_and_noncreating_transactions_leave_tree_unchanged(self):
        def snapshot():
            result = {}
            for path in [self.root, *self.root.rglob("*")]:
                info = path.lstat()
                result[str(path.relative_to(self.root))] = (info.st_ino, info.st_mode,
                                                           info.st_size, info.st_mtime_ns)
            return result

        # Cover both a brand-new root and a real order with stable lock inodes.
        for existing in (False, True):
            with self.subTest(existing_order=existing):
                if existing:
                    self.create()
                empty_id = uuid.uuid4().hex
                (self.root / empty_id).mkdir()  # Uploaded directory, no committed order.
                before = snapshot()
                ids = [uuid.uuid4().hex for _ in range(100)] + [empty_id]
                with patch.object(self.repository, "lock", side_effect=AssertionError("Missing read acquired a lock")):
                    for job_id in ids:
                        self.assertIsNone(self.repository.get(job_id))
                        with self.repository.transaction(job_id, create=False) as job:
                            self.assertIsNone(job)
                self.assertEqual(snapshot(), before)

    def test_first_creation_is_absent_until_commit_then_visible_without_waiting_on_missing_read(self):
        entered, release = threading.Event(), threading.Event()
        errors = queue.Queue()
        writer = RepairRepository(self.root)

        def create():
            try:
                with writer.transaction(JOB_ID, create=True) as job:
                    job.update(status="paid", expected_amount="5.99")
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("Creation was not released")
            except BaseException as exc:
                errors.put(exc)

        thread = threading.Thread(target=create)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            lockfile = self.root / ".repair-locks" / ("order-" + JOB_ID + ".lock")
            inode = lockfile.stat().st_ino
            with patch.object(self.repository, "lock", side_effect=AssertionError("Missing read waited for creator")):
                self.assertIsNone(self.repository.get(JOB_ID))
                with self.repository.transaction(JOB_ID) as job:
                    self.assertIsNone(job)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors.empty(), list(errors.queue))
        self.assertEqual(self.repository.get(JOB_ID), {"status": "paid", "expected_amount": "5.99"})
        self.assertEqual(lockfile.stat().st_ino, inode)

    def test_existing_order_preflight_does_not_replace_locked_fresh_read(self):
        self.create()
        writer = RepairRepository(self.root)
        lockfile = self.root / ".repair-locks" / ("order-" + JOB_ID + ".lock")
        inode = lockfile.stat().st_ino
        for method in ("get", "transaction"):
            with self.subTest(method=method):
                with writer.transaction(JOB_ID) as job:
                    job["status"] = "pending_payment"
                entered, release = threading.Event(), threading.Event()
                results, errors = [], queue.Queue()
                original = self.repository._order_exists

                def preflight(*args):
                    result = original(*args)
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("Read preflight was not released")
                    return result

                def read():
                    try:
                        if method == "get":
                            results.append(self.repository.get(JOB_ID))
                        else:
                            with self.repository.transaction(JOB_ID) as job:
                                results.append(copy.deepcopy(job))
                    except BaseException as exc:
                        errors.put(exc)

                with patch.object(self.repository, "_order_exists", side_effect=preflight):
                    thread = threading.Thread(target=read)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        with writer.transaction(JOB_ID) as job:
                            job.update(status="paid", payment_source="verified_webhook")
                    finally:
                        release.set()
                        thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(errors.empty(), list(errors.queue))
                self.assertEqual(results[0]["status"], "paid")
                self.assertEqual(results[0]["expected_amount"], "0.01")
                self.assertEqual(results[0]["payment_source"], "verified_webhook")
                self.assertEqual(lockfile.stat().st_ino, inode)

    def test_body_exception_aborts_existing_and_new_order(self):
        self.create()
        before = self.metadata().read_bytes()
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.repository.transaction(JOB_ID) as job:
                job["status"] = "paid"
                raise RuntimeError("abort")
        self.assertEqual(self.metadata().read_bytes(), before)
        with self.assertRaises(RuntimeError):
            with self.repository.transaction(OTHER_ID, create=True) as job:
                job["status"] = "paid"
                raise RuntimeError("abort")
        self.assertFalse((self.root / OTHER_ID).exists())

    def test_failed_replace_leaves_old_document_and_cleans_own_temporary(self):
        self.create()
        before = self.metadata().read_bytes()
        unrelated = self.metadata().parent / ".other.tmp"
        unrelated.write_bytes(b"untouched")
        with patch("app.domain.repair_repository.os.replace", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                with self.repository.transaction(JOB_ID) as job:
                    job["status"] = "paid"
        self.assertEqual(self.metadata().read_bytes(), before)
        self.assertEqual(list(self.metadata().parent.glob(".order.json-*.tmp")), [])
        self.assertEqual(unrelated.read_bytes(), b"untouched")

    def test_read_and_noop_transaction_preserve_original_bytes_and_mtime(self):
        # Also proves old pretty-printed metadata is not silently rewritten.
        self.raw_metadata('{\n "status": "paid", "expected_amount": "5.99"\n}\n')
        before = (self.metadata().read_bytes(), self.metadata().stat().st_mtime_ns)
        self.assertEqual(self.repository.get(JOB_ID)["status"], "paid")
        with self.repository.transaction(JOB_ID) as job:
            self.assertEqual(job["expected_amount"], "5.99")
        self.assertEqual((self.metadata().read_bytes(), self.metadata().stat().st_mtime_ns), before)

    def test_invalid_ids_are_never_resolved_as_paths(self):
        for job_id in (None, 12, "", "A" * 32, "1" * 31, "../outside", JOB_ID + "/extra"):
            with self.subTest(job_id=job_id):
                self.assertIsNone(self.repository.get(job_id))
                with self.assertRaises(ValueError):
                    with self.repository.transaction(job_id, create=True):
                        self.fail("Invalid ID entered a transaction")

    def test_corrupt_nonobject_and_nonfinite_metadata_fail_closed(self):
        for raw in ("{broken", "[]", "null", "1", '{"amount": NaN}', '{"amount": Infinity}'):
            with self.subTest(raw=raw):
                self.raw_metadata(raw)
                with self.assertRaises(RepairMetadataError):
                    self.repository.get(JOB_ID)
                with self.assertRaises(RepairMetadataError):
                    with self.repository.transaction(JOB_ID, create=False):
                        self.fail("Corrupt metadata entered noncreating transaction")
                with self.assertRaises(RepairMetadataError):
                    with self.repository.transaction(JOB_ID, create=True) as job:
                        job.clear()
                self.assertEqual(self.metadata().read_text(), raw)

    def test_invalid_changed_value_does_not_replace_valid_metadata(self):
        self.create()
        before = self.metadata().read_bytes()
        for invalid in (float("nan"), object()):
            with self.subTest(invalid=type(invalid)):
                with self.assertRaises(RepairMetadataError):
                    with self.repository.transaction(JOB_ID) as job:
                        job["invalid"] = invalid
                self.assertEqual(self.metadata().read_bytes(), before)

    def test_unsafe_known_filenames_fail_on_load_and_commit(self):
        for key in ("filename", "download_filename", "artifact_file"):
            for unsafe in ("..", ".", "../outside.epub", "/tmp/book.epub", "a\\b.epub", "a\0b", 1):
                with self.subTest(key=key, unsafe=unsafe):
                    self.raw_metadata(json.dumps({key: unsafe}))
                    with self.assertRaises(RepairMetadataError):
                        self.repository.get(JOB_ID)
        self.raw_metadata('{"status":"pending_payment"}')
        before = self.metadata().read_bytes()
        with self.assertRaises(RepairMetadataError):
            with self.repository.transaction(JOB_ID) as job:
                job["filename"] = "../secret.epub"
        self.assertEqual(self.metadata().read_bytes(), before)

    def test_job_directory_symlink_is_not_followed(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        original = outside / "order.json"
        original.write_text('{"status":"paid"}')
        (self.root / JOB_ID).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RepairMetadataError):
            self.repository.get(JOB_ID)
        with self.assertRaises(RepairMetadataError):
            with self.repository.transaction(JOB_ID, create=False):
                self.fail("Symlink directory entered noncreating transaction")
        with self.assertRaises(RepairMetadataError):
            with self.repository.transaction(JOB_ID, create=True):
                self.fail("Symlink directory entered transaction")
        self.assertEqual(original.read_text(), '{"status":"paid"}')

    def test_metadata_symlink_and_fifo_are_not_followed(self):
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text('{"status":"paid"}')
        self.metadata().parent.mkdir()
        self.metadata().symlink_to(outside)
        with self.assertRaises(RepairMetadataError):
            self.repository.get(JOB_ID)
        with self.assertRaises(RepairMetadataError):
            with self.repository.transaction(JOB_ID, create=False):
                self.fail("Symlink metadata entered noncreating transaction")
        self.metadata().unlink()
        os.mkfifo(self.metadata())
        with self.assertRaises(RepairMetadataError):
            self.repository.get(JOB_ID)
        with self.assertRaises(RepairMetadataError):
            with self.repository.transaction(JOB_ID, create=False):
                self.fail("FIFO metadata entered noncreating transaction")
        self.assertEqual(outside.read_text(), '{"status":"paid"}')

    def test_source_symlink_is_rejected_and_input_is_never_modified(self):
        self.create()
        source = self.metadata().parent / "book.epub"
        artifact = self.metadata().parent / "book_fixed.epub"
        source.write_bytes(b"immutable original source")
        artifact.write_bytes(b"prior artifact")
        with self.repository.transaction(JOB_ID) as job:
            job.update(status="paid", download_filename=artifact.name)
        self.assertEqual(source.read_bytes(), b"immutable original source")
        self.assertEqual(artifact.read_bytes(), b"prior artifact")
        source.unlink()
        source.symlink_to(artifact)
        with self.assertRaises(RepairMetadataError):
            self.repository.get(JOB_ID)
        self.assertEqual(artifact.read_bytes(), b"prior artifact")

    def test_root_and_lock_directory_symlinks_are_rejected(self):
        alias = Path(self.temporary.name) / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RepairMetadataError):
            RepairRepository(alias)
        outside = Path(self.temporary.name) / "outside-locks"
        outside.mkdir()
        (self.root / ".repair-locks").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RepairMetadataError):
            with self.repository.lock("execution-slot-0"):
                self.fail("Symlink locks directory was followed")
        self.assertEqual(list(outside.iterdir()), [])

    def test_named_lock_validation_and_symlink_file_guard(self):
        for name in (None, "", "../outside", "/tmp/outside", "x" * 129):
            with self.subTest(name=name), self.assertRaises(ValueError):
                with self.repository.lock(name):
                    self.fail("Invalid named lock")
        locks = self.root / ".repair-locks"
        locks.mkdir()
        outside = Path(self.temporary.name) / "lock-target"
        outside.write_bytes(b"not a lock")
        (locks / "execution-slot-0.lock").symlink_to(outside)
        with self.assertRaises(RepairMetadataError):
            with self.repository.lock("execution-slot-0"):
                self.fail("Symlink lock was followed")
        self.assertEqual(outside.read_bytes(), b"not a lock")

    def test_file_and_directory_are_fsynced_on_commit(self):
        calls = []
        original = os.fsync

        def checked(fd):
            calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return original(fd)

        with patch("app.domain.repair_repository.os.fsync", side_effect=checked):
            self.create()
        self.assertIn(False, calls)
        self.assertTrue(calls[-1], "Commit must fsync the containing directory after rename")

    def test_parallel_process_transactions_have_no_lost_updates(self):
        self.create()
        context = multiprocessing.get_context("spawn")
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=increment_worker,
                                     args=(str(self.root), ready, start, results, 20)) for _ in range(3)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            self.assertEqual([results.get(timeout=15) for _ in processes], [None] * 3)
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            ready.close()
            results.close()
        current = self.repository.get(JOB_ID)
        self.assertEqual(current["count"], 60)
        self.assertEqual(current["expected_amount"], "0.01")

    def test_real_process_named_lock_contention_keeps_stable_inode(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Queue(), context.Event()
        process = context.Process(target=lock_worker, args=(str(self.root), ready, release))
        process.start()
        try:
            self.assertTrue(ready.get(timeout=10))
            lockfile = self.root / ".repair-locks" / "execution-slot-0.lock"
            inode = lockfile.stat().st_ino
            with self.repository.lock("execution-slot-0", blocking=False) as acquired:
                self.assertFalse(acquired)
            release.set()
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            with self.repository.lock("execution-slot-0", blocking=False) as acquired:
                self.assertTrue(acquired)
                self.assertEqual(lockfile.stat().st_ino, inode)
            self.assertEqual(lockfile.stat().st_ino, inode)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            ready.close()

    def test_named_lock_can_be_released_by_another_thread(self):
        held = ExitStack()
        self.assertTrue(held.enter_context(self.repository.lock("execution-slot-0", blocking=False)))
        with self.repository.lock("execution-slot-0", blocking=False) as acquired:
            self.assertFalse(acquired)
        errors = queue.Queue()

        def release():
            try:
                held.close()
            except BaseException as exc:
                errors.put(exc)

        thread = threading.Thread(target=release)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors.empty())
        with self.repository.lock("execution-slot-0", blocking=False) as acquired:
            self.assertTrue(acquired)

    def test_gateway_budget_is_shared_persistent_and_readonly_when_throttled(self):
        self.assertTrue(self.repository.reserve_gateway(100))
        saved = self.root / ".repair-gateway.json"
        before = (saved.read_bytes(), saved.stat().st_mtime_ns)
        other = RepairRepository(self.root)
        self.assertFalse(other.reserve_gateway(100.5))
        self.assertFalse(other.reserve_gateway(99))
        self.assertEqual((saved.read_bytes(), saved.stat().st_mtime_ns), before)
        self.assertTrue(other.reserve_gateway(101))
        with other.lock("payment-query", blocking=False) as acquired:
            self.assertTrue(acquired)
            self.assertTrue(other.reserve_gateway(102), "Budget lock must differ from outer query lock")

    def test_parallel_gateway_reservations_have_one_winner(self):
        context = multiprocessing.get_context("spawn")
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=budget_worker,
                                     args=(str(self.root), ready, start, results)) for _ in range(3)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            outcomes = [results.get(timeout=10) for _ in processes]
            self.assertTrue(all(type(value) is bool for value in outcomes), outcomes)
            self.assertEqual(sorted(outcomes), [False, False, True])
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            ready.close()
            results.close()

    def test_invalid_gateway_inputs_and_corrupt_budget_fail_closed(self):
        for now, interval in ((True, 1), (float("nan"), 1), (100, 0), (100, -1), (100, "1")):
            with self.subTest(now=now, interval=interval), self.assertRaises(ValueError):
                self.repository.reserve_gateway(now, interval)
        path = self.root / ".repair-gateway.json"
        for raw in ("{}", "[]", "{broken", '{"last_gateway_check_at":"100"}'):
            path.write_text(raw)
            with self.assertRaises(RepairMetadataError):
                self.repository.reserve_gateway(100)
            self.assertEqual(path.read_text(), raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
