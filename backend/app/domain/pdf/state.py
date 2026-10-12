"""Private checkpoints and conservative rolling quota for workers sharing one disk."""
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from .config import PdfError, root


def job_dir(job_id):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', job_id):
        raise PdfError('无效 PDF 任务编号')
    path = root() / job_id
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def save(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        raise PdfError('PDF 检查点损坏，已停止新增 OCR 请求，请联系管理员') from None


@contextmanager
def job_lock(job_id):
    with (job_dir(job_id) / '.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PdfError('PDF 任务已在处理中，请勿重复执行') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Quota:
    def __init__(self, key, path=None):
        # A key rotation must NOT reset the account quota. Default to one shared
        # conservative bucket; a nonsecret account id can distinguish real accounts.
        self.account = hashlib.sha256(os.environ.get("MINERU_QUOTA_ACCOUNT", "default-account").encode()).hexdigest()
        self.path = Path(path or root() / 'mineru-quota.sqlite3')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reservations (account TEXT, job TEXT, pages INTEGER, created REAL, PRIMARY KEY(account,job))')
            db.execute('CREATE TABLE IF NOT EXISTS circuits (account TEXT PRIMARY KEY, until REAL)')
        os.chmod(self.path, 0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def reserve(self, job, pages, limit=1000, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM reservations WHERE account=? AND job=?', (self.account, job)).fetchone():
                return True
            if db.execute('SELECT 1 FROM circuits WHERE account=? AND until>?', (self.account, now)).fetchone():
                return False
            used = db.execute('SELECT COALESCE(SUM(pages),0) FROM reservations WHERE account=? AND created>?', (self.account, now - 86400)).fetchone()[0]
            if used + pages > min(1000, max(0, limit)):
                return False
            db.execute('INSERT INTO reservations VALUES(?,?,?,?)', (self.account, job, pages, now))
            return True

    def open_circuit(self):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO circuits VALUES(?,?)', (self.account, time.time() + 600))


@contextmanager
def file_lock(path):
    """flock uses separate open descriptions: serializes threads AND worker processes."""
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def reserve_request(path, limit):
    path = Path(path)
    with file_lock(path.with_suffix('.lock')):
        counter = read(path, {'requests': 0})
        if type(counter.get('requests')) is not int or counter['requests'] < 0:
            raise PdfError('PDF OCR 请求计数损坏，已停止新增请求')
        if counter['requests'] >= limit:
            raise PdfError('PDF OCR 已到本任务请求上限；检查点已保存，请联系管理员')
        counter['requests'] += 1
        save(path, counter)
