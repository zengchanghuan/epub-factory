"""Fresh, atomic repair metadata transactions on one shared local filesystem.

The existing ``<job>/order.json`` remains authoritative. Stable flock inodes are
separate from atomically replaced data files. This is a same-host/local-volume
contract, not a distributed lock for NFS. Do not nest a transaction/get for the
same order: file locks are deliberately not reentrant.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid


_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
_LOCK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class RepairMetadataError(RuntimeError):
    """Metadata is corrupt or an unsafe filesystem entry was encountered."""


def _finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON value: {value}")


class RepairRepository:
    def __init__(self, root):
        supplied = Path(root).expanduser()
        if supplied.is_symlink():
            raise RepairMetadataError("Repair root must not be a symlink")
        self.root = supplied.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._directory(self.root):
            pass

    @staticmethod
    def _valid_id(job_id):
        return isinstance(job_id, str) and _JOB_ID.fullmatch(job_id) is not None

    @contextmanager
    def _directory(self, path, *, parent_fd=None):
        try:
            fd = os.open(path, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise
            raise RepairMetadataError("Unsafe repair directory") from exc
        try:
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def lock(self, name, blocking=True):
        """Yield whether a stable named exclusive lock was acquired.

        FD ownership alone controls this lease; a caller may transfer its
        context/ExitStack to another thread for release. Never unlink lock files.
        """
        if not isinstance(name, str) or not _LOCK_NAME.fullmatch(name):
            raise ValueError("Invalid repair lock name")
        with self._directory(self.root) as root_fd:
            try:
                os.mkdir(".repair-locks", 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            with self._directory(".repair-locks", parent_fd=root_fd) as lock_dir:
                try:
                    # Separate existing-open from exclusive creation: the
                    # first-use race is resolved without replacing any inode
                    # or relying on a combined O_CREAT|O_NOFOLLOW open.
                    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
                    try:
                        fd = os.open(name + ".lock", flags, dir_fd=lock_dir)
                    except FileNotFoundError:
                        try:
                            fd = os.open(name + ".lock", flags | os.O_CREAT | os.O_EXCL,
                                         0o600, dir_fd=lock_dir)
                        except FileExistsError:
                            fd = os.open(name + ".lock", flags, dir_fd=lock_dir)
                except OSError as exc:
                    raise RepairMetadataError("Unsafe repair lock file") from exc
                acquired = False
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise RepairMetadataError("Repair lock must be a regular file")
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                        acquired = True
                    except OSError as exc:
                        if blocking or exc.errno not in (errno.EAGAIN, errno.EACCES):
                            raise
                    yield acquired
                finally:
                    if acquired:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    @staticmethod
    def _read_json(directory_fd, filename):
        try:
            fd = os.open(filename, _READ_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RepairMetadataError("Unsafe repair metadata file") from exc
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RepairMetadataError("Repair metadata must be a regular file")
                value = json.load(stream, parse_constant=_reject_constant)
        except (ValueError, UnicodeError, OSError) as exc:
            raise RepairMetadataError("Invalid repair metadata JSON") from exc
        if not isinstance(value, dict):
            raise RepairMetadataError("Repair metadata must be a JSON object")
        return value

    @staticmethod
    def _validate_order(value, directory_fd=None):
        # Leave all pricing, payment, execution and legacy fields untouched.
        # Only the fields interpreted as local paths have a storage constraint.
        for key in ("filename", "download_filename", "artifact_file"):
            filename = value.get(key)
            if filename is None or filename == "":
                continue
            if (not isinstance(filename, str) or filename in (".", "..")
                    or "/" in filename or "\\" in filename or "\x00" in filename):
                raise RepairMetadataError(f"Unsafe repair {key}")
            if directory_fd is not None:
                try:
                    info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    raise RepairMetadataError(f"Symlink repair {key}")

    @staticmethod
    def _serialized(value):
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RepairMetadataError("Repair metadata is not JSON serializable") from exc

    @staticmethod
    def _atomic_json(directory_fd, filename, payload):
        # Serialization occurs before this call; exceptions before replace leave
        # the prior document intact. A post-rename fsync failure has an unknown
        # durability outcome and must propagate rather than pretend rollback.
        temporary = "." + filename + "-" + uuid.uuid4().hex + ".tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def _read_order(self, root_fd, job_id):
        try:
            with self._directory(job_id, parent_fd=root_fd) as directory_fd:
                value = self._read_json(directory_fd, "order.json")
                if value is not None:
                    self._validate_order(value, directory_fd)
                return value
        except FileNotFoundError:
            return None

    def _order_exists(self, root_fd, job_id):
        """No-write preflight; existing records are still re-read under lock.

        A concurrent first commit may make an absent order visible immediately
        after this check. Returning absent linearizes before that commit; it
        must not allocate a permanent lock for an arbitrary public lookup.
        """
        try:
            with self._directory(job_id, parent_fd=root_fd) as directory_fd:
                try:
                    info = os.stat("order.json", dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                except OSError as exc:
                    raise RepairMetadataError("Unsafe repair metadata file") from exc
                if not stat.S_ISREG(info.st_mode):
                    raise RepairMetadataError("Repair metadata must be a regular file")
                return True
        except FileNotFoundError:
            return False

    def get(self, job_id):
        """Read a fresh detached snapshot; never mutate order JSON on reads."""
        if not self._valid_id(job_id):
            return None
        with self._directory(self.root) as root_fd:
            if not self._order_exists(root_fd, job_id):
                return None
        with self.lock("order-" + job_id):
            with self._directory(self.root) as root_fd:
                return copy.deepcopy(self._read_order(root_fd, job_id))

    @contextmanager
    def transaction(self, job_id, create=False):
        """Read latest under lock, then commit only a successful changed body."""
        if not self._valid_id(job_id):
            raise ValueError("Invalid repair job ID")
        if not create:
            with self._directory(self.root) as root_fd:
                if not self._order_exists(root_fd, job_id):
                    yield None
                    return
        with self.lock("order-" + job_id):
            with self._directory(self.root) as root_fd:
                original = self._read_order(root_fd, job_id)
                current = copy.deepcopy(original) if original is not None else ({} if create else None)
                yield current
                if current is None or (original is not None and current == original):
                    return
                self._validate_order(current)
                payload = self._serialized(current)
                try:
                    os.mkdir(job_id, 0o700, dir_fd=root_fd)
                    os.fsync(root_fd)
                except FileExistsError:
                    pass
                with self._directory(job_id, parent_fd=root_fd) as directory_fd:
                    self._validate_order(current, directory_fd)
                    self._atomic_json(directory_fd, "order.json", payload)

    def reserve_gateway(self, now, interval_seconds=1):
        """Reserve the persisted cross-process gateway budget; no network I/O.

        The optional outer ``payment-query`` lease may span the actual query.
        This separate budget lock is never nested with itself.
        """
        now = _finite_number(now, "now")
        interval = _finite_number(interval_seconds, "interval_seconds")
        if interval <= 0:
            raise ValueError("interval_seconds must be positive")
        with self.lock("payment-budget"):
            with self._directory(self.root) as root_fd:
                saved = self._read_json(root_fd, ".repair-gateway.json")
                if saved is not None:
                    try:
                        previous = _finite_number(saved["last_gateway_check_at"], "last_gateway_check_at")
                    except (KeyError, ValueError) as exc:
                        raise RepairMetadataError("Invalid repair gateway budget") from exc
                    if now - previous < interval:
                        return False
                self._atomic_json(root_fd, ".repair-gateway.json",
                                  self._serialized({"last_gateway_check_at": now}))
                return True
