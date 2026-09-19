"""Immediate merchant mail dispatcher independent of book/completion queues."""
import logging
import threading

from .payment_email_service import dispatch_pending_payment_emails, payment_email_capabilities

logger = logging.getLogger("epub_factory")


class PaymentEmailWorker:
    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        if not payment_email_capabilities()["available"]:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="payment-email", daemon=True)
            self._thread.start()
            logger.info("Merchant payment email dispatcher started")

    def wake(self):
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                dispatch_pending_payment_emails(limit=20)
            except Exception:
                logger.warning("Payment email dispatcher unavailable; pending receipts retained")
            self._wake.wait(5)

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)


payment_email_worker = PaymentEmailWorker()
