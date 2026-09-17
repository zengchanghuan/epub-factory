"""Owner-checked, renewable execution lease; fail closed when Redis is unavailable."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from app.cancellation import JobCancelled


class ExecutionLeaseUnavailable(RuntimeError):
    pass


class ExecutionLeaseBusy(RuntimeError):
    """Retry a broker delivery later; the existing owner's lease may expire."""


class ExecutionLeaseLost(JobCancelled):
    pass


_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class LocalExecutionLease:
    def __init__(self):
        self.owner = uuid.uuid4().hex

    def assert_owned(self) -> None:
        pass


class RedisExecutionLease:
    def __init__(self, client, key: str, ttl: int = 300):
        self.client = client
        self.key = key
        self.ttl = ttl
        self.owner = uuid.uuid4().hex
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = None

    def acquire(self) -> bool:
        try:
            return bool(self.client.set(self.key, self.owner, nx=True, ex=self.ttl))
        except Exception as exc:
            raise ExecutionLeaseUnavailable("任务执行锁暂不可用，未启动翻译") from exc

    def renew(self) -> bool:
        try:
            owned = bool(self.client.eval(_RENEW, 1, self.key, self.owner, self.ttl))
        except Exception:
            owned = False
        if not owned:
            self._lost.set()
        return owned

    def start(self) -> None:
        def heartbeat():
            while not self._stop.wait(self.ttl / 3):
                if not self.renew():
                    return
        self._thread = threading.Thread(target=heartbeat, daemon=True)
        self._thread.start()

    def assert_owned(self) -> None:
        if not self._lost.is_set():
            try:
                owner = self.client.get(self.key)
                if isinstance(owner, bytes):
                    owner = owner.decode()
                if owner != self.owner:
                    self._lost.set()
            except Exception:
                self._lost.set()
        if self._lost.is_set():
            raise ExecutionLeaseLost("任务执行锁已失效，停止旧执行器")

    def release(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        try:
            self.client.eval(_RELEASE, 1, self.key, self.owner)
        except Exception:
            # Expiry is the fallback; never delete an unverified owner's lock.
            pass


@contextmanager
def execution_lease(job_id: str, attempt_id: str):
    digest = hashlib.sha256(f"{job_id}:{attempt_id}".encode()).hexdigest()
    redis_url = os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL")
    if redis_url:
        import redis
        try:
            client = redis.Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        except Exception as exc:
            raise ExecutionLeaseUnavailable("任务执行锁暂不可用，未启动翻译") from exc
        lease = RedisExecutionLease(client, f"epub:execution:{digest}")
        acquired = False
        try:
            acquired = lease.acquire()
            if acquired:
                lease.start()
            yield lease if acquired else None
        finally:
            if acquired:
                lease.release()
            client.close()
        return

    # Local BackgroundTasks also need cross-thread/process exclusion. Keep the
    # lock inode after release so opening/unlinking cannot bypass an active lock.
    import fcntl
    lock_dir = Path(tempfile.gettempdir()) / f"epub-execution-{os.getuid()}"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(lock_dir / digest, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        lease = LocalExecutionLease()
        yield lease if acquired else None
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
