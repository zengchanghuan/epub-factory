"""Validated feedback persistence; report success only after durable append."""
from collections import deque
import fcntl
import json
import os
from threading import Lock
import time

FEEDBACK_TYPES = {'translation', 'layout', 'directory', 'other', 'suggestion'}


class FeedbackLimiter:
    """Bounded per-instance spam guard, separate from paid conversion quotas."""
    def __init__(self):
        self.lock, self.entries = Lock(), {}

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            for old in list(self.entries):
                if not self.entries[old] or self.entries[old][-1] < now-60: del self.entries[old]
            if key not in self.entries and len(self.entries) >= 2048: return False
            values = self.entries.setdefault(key, deque())
            while values and values[0] < now-60: values.popleft()
            if len(values) >= 10: return False
            values.append(now)
            return True


feedback_limiter = FeedbackLimiter()


def persist_feedback(path, entry):
    with path.open('a', encoding='utf-8') as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        file.write(json.dumps(entry, ensure_ascii=False) + '\n')
        file.flush()
        os.fsync(file.fileno())
