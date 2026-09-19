"""Small durable-outbox dispatcher independent of the busy book worker."""
import logging
import os
import threading

from .completion_email_service import dispatch_pending_email_notifications, email_capabilities

logger = logging.getLogger("epub_factory")


class CompletionEmailWorker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not email_capabilities().get("available"):
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="completion-email", daemon=True)
        self._thread.start()
        logger.info("Customer completion email dispatcher started")

    def _run(self):
        try:
            interval = max(5, min(300, int(os.environ.get("EMAIL_DISPATCH_INTERVAL_SECONDS", "30"))))
        except ValueError:
            interval = 30
        while not self._stop.is_set():
            try:
                dispatch_pending_email_notifications(limit=20)
            except Exception:
                # Provider exception messages may contain addresses or credentials.
                logger.warning("Completion email dispatcher unavailable; pending notifications retained")
            self._stop.wait(interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
