"""R7 isolated scanner/lease/lifecycle contracts; no broker or model calls."""
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("EPUB_PERSISTENT_STORE", "0")

from app.models import Job, JobStatus, OutputMode
from app.storage import JobStore
from app.domain.job_recovery_service import recover_lost_executions, recovery_config
from app.domain.job_recovery_worker import JobRecoveryWorker
from app.infra.execution_lease import execution_lease, ExecutionLeaseUnavailable


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r7-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.patch(patch.dict(os.environ, {"CELERY_BROKER_URL": "", "REDIS_URL": ""}))
        self.patch(patch("app.infra.execution_lease.tempfile.gettempdir", return_value=self.temp.name))
        self.store = JobStore()
        self.job = Job(id="lost", trace_id="t", source_filename="book.epub", input_path="none.epub",
                       output_mode=OutputMode.simplified, status=JobStatus.pending,
                       translation_stats={"attempt_id": "a", "api_calls": 7, "free_retry_count": 1})
        self.store.add(self.job)
        self.assertTrue(self.store.begin_execution("lost", "a", "dead", now=100))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def recover(self, **options):
        return recover_lost_executions(self.store, now=1000, stale_seconds=600, **options)

    def test_expired_heartbeat_and_unowned_lease_requeues_once(self):
        self.assertEqual(self.recover()["recovered"], 1)
        self.assertEqual(self.recover()["scanned"], 0)
        self.assertEqual(self.store.get("lost").translation_stats["api_calls"], 7)
        self.assertEqual(self.store.get("lost").translation_stats["free_retry_count"], 1)
        self.assertEqual(self.store.list_dispatches("lost")[0]["status"], "pending")

    def test_live_lease_prevents_recovery_even_when_heartbeat_stale(self):
        with execution_lease("lost", "a") as lease:
            self.assertIsNotNone(lease)
            self.assertEqual(self.recover()["busy"], 1)
            self.assertEqual(self.store.get("lost").status, JobStatus.running)
        self.assertEqual(self.recover()["recovered"], 1)

    def test_recent_heartbeat_is_not_selected(self):
        self.store.heartbeat_execution("lost", "a", "dead", now=999)
        self.assertEqual(self.recover()["scanned"], 0)

    def test_legacy_translation_still_holding_pre_upgrade_conversion_lock_is_not_stolen(self):
        from datetime import datetime, timezone
        old = Job(id="old", trace_id="old", source_filename="old.epub", input_path="unused.epub",
                  output_mode=OutputMode.simplified, enable_translation=True,
                  status=JobStatus.running, translation_stats={"attempt_id": "legacy-random-id"},
                  updated_at=datetime.fromtimestamp(100, timezone.utc))
        self.store.add(old)
        with execution_lease("old", "conversion"):
            self.assertEqual(self.recover()["busy"], 1)
            self.assertEqual(self.store.get("old").status, JobStatus.running)
        self.assertEqual(self.recover()["recovered"], 1)

    def test_lease_unavailable_fails_closed(self):
        with patch("app.domain.job_recovery_service.execution_lease", side_effect=ExecutionLeaseUnavailable("secret")):
            self.assertEqual(self.recover()["errors"], 1)
        self.assertEqual(self.store.get("lost").status, JobStatus.running)

    def test_new_heartbeat_between_scan_and_recovery_wins(self):
        @contextmanager
        def racing(*_):
            self.store.heartbeat_execution("lost", "a", "dead", now=999)
            yield Mock(spec=["assert_owned"])
        with patch("app.domain.job_recovery_service.execution_lease", racing):
            self.assertEqual(self.recover()["unchanged"], 1)
        self.assertEqual(self.store.get("lost").status, JobStatus.running)

    def test_cancelled_between_scan_and_recovery_wins(self):
        @contextmanager
        def racing(*_):
            self.store.update_status("lost", JobStatus.cancelled, "User cancelled")
            yield Mock(spec=["assert_owned"])
        with patch("app.domain.job_recovery_service.execution_lease", racing):
            self.assertEqual(self.recover()["unchanged"], 1)
        self.assertEqual(self.store.get("lost").status, JobStatus.cancelled)

    def test_two_scanners_only_recover_once(self):
        barrier = threading.Barrier(3)
        results = []
        def scan():
            barrier.wait()
            results.append(self.recover())
        threads = [threading.Thread(target=scan) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(row["recovered"] for row in results), 1)
        self.assertEqual(self.store.get_execution("lost", "a")["recoveries"], 1)

    def test_exhaustion_requires_manual_action(self):
        self.assertEqual(self.recover(max_recoveries=0)["exhausted"], 1)
        current = self.store.get("lost")
        self.assertEqual(current.status, JobStatus.failed)
        self.assertEqual(current.error_code, "WORKER_RECOVERY_EXHAUSTED")
        self.assertFalse(current.output_path)

    def test_loop_is_independent_idempotent_and_stoppable(self):
        called = threading.Event()
        with patch("app.domain.job_recovery_worker.recover_lost_executions",
                   side_effect=lambda *a, **k: {"recovered": 1, "exhausted": 0, "queued_rearmed": 0}):
            worker = JobRecoveryWorker(lambda: self.store, interval_seconds=.1, on_recovered=called.set)
            self.addCleanup(worker.stop)
            self.assertTrue(worker.start())
            self.assertFalse(worker.start())
            self.assertTrue(called.wait(2))
            self.assertTrue(worker.stop())

    def test_invalid_operator_settings_rejected(self):
        for key, value in (("JOB_RECOVERY_STALE_SECONDS", "nan"), ("JOB_RECOVERY_STALE_SECONDS", "1"),
                           ("JOB_RECOVERY_MAX_ATTEMPTS", "11"), ("JOB_RECOVERY_MAX_ATTEMPTS", "-1")):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                with self.assertRaises(ValueError):
                    recovery_config()

    def test_compiler_never_falls_back_on_soft_timeout_or_cancel(self):
        from app.engine.compiler import ExtremeCompiler
        from app.cancellation import JobCancelled
        from billiard.exceptions import SoftTimeLimitExceeded
        for error in (SoftTimeLimitExceeded(), JobCancelled("cancelled")):
            compiler = ExtremeCompiler("unused.epub", str(Path(self.temp.name) / "out.epub"))
            with self.subTest(error=type(error).__name__), patch.object(compiler, "_run_full_pipeline", side_effect=error), \
                    patch.object(compiler, "_run_safe_mode") as fallback:
                with self.assertRaises(type(error)):
                    compiler.run()
                fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
