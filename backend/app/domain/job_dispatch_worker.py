"""Small durable-intent publisher; importing or constructing it performs no IO.

The application starts it only with a configured broker and persistent store.
The synchronous publisher must enforce its own finite network timeout. Stop is
bounded and cannot cancel a broker call already in progress.
"""
from __future__ import annotations

import logging
import math
import threading

from .job_dispatch_service import MAX_BATCH_SIZE, dispatch_pending

logger = logging.getLogger("epub_factory")


def _bounded_seconds(value, minimum, maximum):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Dispatch worker interval must be finite")
    return min(max(number, minimum), maximum)


class JobDispatchWorker:
    def __init__(self, store_provider, publisher, *, interval_seconds=5, batch_size=20):
        self._store_provider = store_provider
        self._publisher = publisher
        self.interval_seconds = _bounded_seconds(interval_seconds, 0.1, 60)
        self.batch_size = min(max(int(batch_size), 1), MAX_BATCH_SIZE)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        """Start once; refuse a second loop while the previous thread lives."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="job-dispatch", daemon=True)
            self._thread.start()
            return True

    def wake(self):
        self._wake.set()

    def stop(self, timeout=1):
        """Request stop and join for at most two seconds; report actual exit."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_bounded_seconds(timeout, 0, 2))
        return thread is None or not thread.is_alive()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                dispatch_pending(self._store_provider(), self._publisher, limit=self.batch_size)
            except Exception as exc:
                # Never print provider exceptions (may include credentials).
                logger.warning("Job dispatch pass unavailable (%s); intents retained", type(exc).__name__[:80])
            self._wake.wait(self.interval_seconds)
