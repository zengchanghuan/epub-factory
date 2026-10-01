"""Progress-independent durable execution heartbeat, fenced by attempt/owner.

No work or network access happens on import. These are engineering intervals,
not liveness guarantees: recovery also has to acquire the execution lease.
"""
from __future__ import annotations

import logging
import math
import os
import threading

logger = logging.getLogger("epub_factory.execution_heartbeat")


class ExecutionHeartbeat:
    def __init__(self, store, job_id: str, attempt_id: str, lease, *, interval_seconds=None):
        self.store = store
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.lease = lease
        raw = (os.environ.get("JOB_EXECUTION_HEARTBEAT_SECONDS", "15")
               if interval_seconds is None else interval_seconds)
        try:
            interval = float(raw)
            if not math.isfinite(interval):
                interval = 15.0
        except (TypeError, ValueError):
            interval = 15.0
        self.interval_seconds = max(0.05 if interval_seconds is not None else 1.0,
                                    min(20.0, interval))
        self._stop = threading.Event()
        self._thread = None

    def pulse(self) -> bool:
        if self._stop.is_set():
            return False
        try:
            self.lease.assert_owned()
            owned = self.store.heartbeat_execution(
                self.job_id, self.attempt_id, self.lease.owner)
            if not owned:
                raise RuntimeError("durable execution owner no longer matches")
            return True
        except Exception as exc:
            self.lease.mark_lost()
            self._stop.set()
            # Do not include DB connection strings, provider content or errors.
            logger.warning("Execution heartbeat lost ownership (%s)", type(exc).__name__,
                           extra={"job_id": self.job_id})
            return False

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            return

        def run():
            while not self._stop.wait(self.interval_seconds):
                if not self.pulse():
                    return

        self._thread = threading.Thread(target=run, name="epub-execution-heartbeat", daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds=2.0) -> bool:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0.0, min(2.0, float(timeout_seconds))))
        return self._thread is None or not self._thread.is_alive()
