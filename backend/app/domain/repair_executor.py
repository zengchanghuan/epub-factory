"""Bounded repair threads with same-host, shared-volume execution admission.

The repository supplies persistent, non-unlinked flock files. These are not a
distributed lock and the thread pool is not an independent OS worker process.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import re
import stat
import threading
import uuid


logger = logging.getLogger("epub_factory.repair_executor")
_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")


class RepairExecutor:
    def __init__(self, concurrency: int = 1):
        if type(concurrency) is not int or not 1 <= concurrency <= 4:
            raise ValueError("Repair concurrency must be an integer from 1 to 4")
        self.concurrency = concurrency
        self._pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="epub-repair")
        self._capacity = threading.BoundedSemaphore(concurrency)
        self._state_lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def _configuration_matches(self, repository) -> bool:
        """Pin one concurrency for all executors using this shared directory."""
        with repository.lock("execution-config", blocking=False) as locked:
            if not locked:
                return False
            root = Path(repository.root)
            path = root / ".repair-executor.json"
            expected = {"version": 1, "concurrency": self.concurrency}
            flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                fd = os.open(path, flags)
            except FileNotFoundError:
                temporary = root / (".repair-executor-" + uuid.uuid4().hex + ".tmp")
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as target:
                        json.dump(expected, target, sort_keys=True)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, path)
                    directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                                           getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    temporary.unlink(missing_ok=True)
                return True
            else:
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError("Repair executor configuration is not a regular file")
                    raw = source.read(1025)
                if len(raw) > 1024:
                    raise ValueError("Repair executor configuration is invalid")
                saved = json.loads(raw)
                if (not isinstance(saved, dict) or type(saved.get("version")) is not int
                        or type(saved.get("concurrency")) is not int
                        or saved != expected):
                    logger.warning("Repair executor configuration mismatch; refusing admission (requested concurrency=%d)",
                                   self.concurrency)
                    return False
                return True

    def _schedule(self, repository, job_id: str, runner):
        """Admit immediately or leave the persisted paid order for a later tick.

        The local permit bounds outstanding futures; the per-job and global
        slot flocks cover the entire callback, including its terminal commit.
        """
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id) or not callable(runner):
            return None
        if self.closed or not self._capacity.acquire(blocking=False):
            return None
        held = ExitStack()
        transferred = False
        submission_ready = threading.Event()
        try:
            try:
                configured = self._configuration_matches(repository)
            except Exception as exc:
                logger.warning("Repair executor configuration unavailable (%s); refusing admission", type(exc).__name__)
                return None
            if not configured:
                return None
            if not held.enter_context(repository.lock("execution-job-" + job_id, blocking=False)):
                return None
            for index in range(self.concurrency):
                if held.enter_context(repository.lock("execution-slot-" + str(index), blocking=False)):
                    break
            else:
                return None
            job = repository.get(job_id)
            if not isinstance(job, dict) or job.get("status") != "paid":
                return None
            owner = uuid.uuid4().hex

            def execute():
                # ThreadPoolExecutor queues its work item before starting a
                # native thread. If start() raises, submit() can fail while a
                # later worker still receives that orphaned item. Only a
                # positively completed handoff owns the leases/permit.
                submission_ready.wait()
                if not transferred:
                    return
                try:
                    # Re-read after scheduling; never turn a concurrent cancel
                    # or terminal transition into an authorized execution.
                    current = repository.get(job_id)
                    if isinstance(current, dict) and current.get("status") == "paid":
                        runner(repository, job_id, owner)
                except Exception as exc:
                    # Metadata/state policy belongs to the runner. Do not turn
                    # an unknown failure into unpaid or overwrite a newer owner.
                    logger.warning("Repair runner stopped (%s)", type(exc).__name__)
                finally:
                    try:
                        held.close()
                    finally:
                        self._capacity.release()

            with self._state_lock:
                if self._closed:
                    return None
                try:
                    future = self._pool.submit(execute)
                except Exception:
                    # Retire the failed pool: repeated native start failures
                    # must not accumulate an unbounded orphan work queue. Do
                    # not cancel any already accepted sibling work.
                    self._closed = True
                    self._pool.shutdown(wait=False, cancel_futures=False)
                    raise
                transferred = True
            return future
        except Exception as exc:
            logger.warning("Repair executor admission unavailable (%s); paid order retained", type(exc).__name__)
            return None
        finally:
            submission_ready.set()
            if not transferred:
                try:
                    held.close()
                finally:
                    self._capacity.release()

    def submit(self, repository, job_id: str, runner) -> bool:
        """Nonblocking admission; False leaves the paid order in its queue."""
        return self._schedule(repository, job_id, runner) is not None

    def run_inline(self, repository, job_id: str, runner) -> bool:
        """Synchronous maintenance compatibility with exactly the same locks.

        Wait for the bounded pool callback rather than starting an untracked
        caller-thread execution; shutdown also waits for this admitted work.
        """
        future = self._schedule(repository, job_id, runner)
        if future is None:
            return False
        future.result()
        return True

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work; never cancel or forcibly kill admitted threads."""
        with self._state_lock:
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=False)
