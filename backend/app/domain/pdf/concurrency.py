"""Small OCR waves: bounded memory, inherited accounting scope, durable in-flight work."""
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
from itertools import islice
from threading import Event, Lock


def ordered_pages(items, work, *, workers, cancel=lambda: None):
    """work(item, check) persists its result before return. Never abandon paid work.

    At most one wave is scheduled. Fatal errors stop future waves and cooperative
    checks (including split OCR); already-issued requests are drained. Results and
    caller progress callbacks stay in original page order on the calling thread.
    """
    if not 1 <= workers <= 4:
        raise ValueError('PDF OCR workers must be between 1 and 4')
    stopped = Event(); failures = []; lock = Lock()
    def check():
        if stopped.is_set():
            with lock: error = failures[0]
            raise error
        cancel()
    def run(item):
        try:
            check()
            return work(item, check)
        except BaseException as exc:
            with lock:
                if not failures: failures.append(exc)
                stopped.set()
            raise
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='pdf-page') as pool:
        while True:
            check()
            batch = list(islice(iterator, workers))
            if not batch: return
            futures = [pool.submit(copy_context().run, run, item) for item in batch]
            wait(futures)
            if failures: raise failures[0]
            for future in futures: yield future.result()
