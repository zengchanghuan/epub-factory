"""Independent, bounded recovery loop; never consumes the occupied book queue."""
from __future__ import annotations

import logging
import threading

from .job_dispatch_worker import _bounded_seconds
from .job_recovery_service import recover_lost_executions, recovery_config

logger = logging.getLogger("epub_factory")


class JobRecoveryWorker:
    def __init__(self, store_provider, *, interval_seconds=30, on_recovered=None):
        self._store_provider = store_provider
        self.interval_seconds = _bounded_seconds(interval_seconds, 0.1, 60)
        self._on_recovered = on_recovered
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            # Fail early on invalid operator configuration, before any IO.
            self._stale_seconds, self._max_recoveries, self._queue_grace = recovery_config()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="job-recovery", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=1):
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_bounded_seconds(timeout, 0, 2))
        return thread is None or not thread.is_alive()

    def _run(self):
        while not self._stop.is_set():
            try:
                counts = recover_lost_executions(
                    self._store_provider(), stale_seconds=self._stale_seconds,
                    max_recoveries=self._max_recoveries,
                    queue_grace_seconds=self._queue_grace,
                )
                if (counts["recovered"] or counts["queued_rearmed"]) and self._on_recovered:
                    self._on_recovered()
                if counts["recovered"] or counts["exhausted"] or counts["queued_rearmed"]:
                    logger.info("Execution recovery: recovered=%s exhausted=%s queued_rearmed=%s",
                                counts["recovered"], counts["exhausted"], counts["queued_rearmed"])
            except Exception as exc:
                logger.warning("Execution recovery pass unavailable (%s)", type(exc).__name__[:80])
            self._stop.wait(self.interval_seconds)
