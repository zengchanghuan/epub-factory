"""Small API-side payment reconciler independent of book/Celery workers."""
import logging
import threading

logger = logging.getLogger("epub_factory")


class RepairPaymentWorker:
    def __init__(self, tick):
        self._tick = tick
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="repair-payments", daemon=True)
            self._thread.start()
        logger.info("Repair payment reconciler started")

    def _run(self):
        # Initial delay leaves application startup free of gateway work.
        while not self._stop.wait(5):
            try:
                self._tick()
            except Exception:
                logger.warning("Repair payment reconciliation unavailable; orders retained")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
