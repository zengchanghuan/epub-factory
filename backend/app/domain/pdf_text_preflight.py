"""Offline PDF text *candidate* diagnostics, never a payment/coverage approval.

Only this file runs in the isolated child; neither side imports the application.
Limits are engineering bounds, not evidence of reliable extraction or reading order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time


_SCHEMA = "pdf-text-preflight-v1"
_PARSER_VERSION = "6.0.0"
_MESSAGES = {
    "invalid_limits": "PDF 预检限额无效。",
    "invalid_source": "PDF 原稿必须是可读取的普通文件，不允许符号链接。",
    "source_changed": "PDF 原稿在预检期间发生变化，请重新选择文件。",
    "file_limit": "PDF 原稿超过本次预检的文件大小限额。",
    "page_limit": "PDF 页数超过本次预检的页数限额。",
    "text_limit": "PDF 提取文字超过本次预检的统计限额。",
    "result_limit": "PDF 预检结果超过本次预检的大小限额。",
    "timeout": "PDF 预检超时，未获得可用的完整统计。",
    "cancelled": "PDF 预检已取消。",
    "encrypted_pdf": "PDF 无法用空密码打开，本阶段不接收或尝试其他密码。",
    "extraction_forbidden": "PDF 的文字提取权限不允许或无法确认，已停止预检。",
    "corrupt_pdf": "PDF 无法严格解析，未获得可用的完整统计。",
    "empty_pdf": "PDF 没有页面。",
    "parser_unavailable": "本地 PDF 解析器版本不可用。",
    "worker_failed": "隔离的 PDF 预检进程失败。",
    "invalid_result": "隔离的 PDF 预检结果无效。",
}
_PAGE_FLAGS = ("no_extractable_text", "sparse_text", "replacement_characters",
               "control_characters", "private_use_characters", "parser_warnings")
_BOOK_FLAGS = ("parser_warnings", "empty_password_encryption", "memory_limit_unavailable")


class PdfPreflightError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason if reason in _MESSAGES else "worker_failed"
        super().__init__(_MESSAGES[self.reason])


@dataclass(frozen=True)
class PdfPreflightLimits:
    max_file_bytes: int = 50 * 1024 * 1024
    max_pages: int = 500
    timeout_seconds: float = 30.0
    max_page_chars: int = 1_000_000
    max_total_chars: int = 10_000_000
    max_result_bytes: int = 2 * 1024 * 1024
    min_text_chars: int = 40


def _validate_limits(limits):
    if not isinstance(limits, PdfPreflightLimits):
        raise PdfPreflightError("invalid_limits")
    maxima = {"max_file_bytes": 200 * 1024 * 1024, "max_pages": 2000,
              "max_page_chars": 2_000_000, "max_total_chars": 20_000_000,
              "max_result_bytes": 8 * 1024 * 1024, "min_text_chars": 1000}
    for name, maximum in maxima.items():
        value = getattr(limits, name)
        if type(value) is not int or not 1 <= value <= maximum:
            raise PdfPreflightError("invalid_limits")
    seconds = limits.timeout_seconds
    if (type(seconds) not in (int, float) or not math.isfinite(seconds)
            or not 0 < seconds <= 300 or limits.max_result_bytes < 128):
        raise PdfPreflightError("invalid_limits")
    return limits


def _check(deadline, cancel_check):
    if cancel_check is not None and cancel_check():
        raise PdfPreflightError("cancelled")
    if time.monotonic() >= deadline:
        raise PdfPreflightError("timeout")


def _open_source(path):
    """Walk directory descriptors to avoid symlink traversal / path-swap races."""
    directory = None
    try:
        path = Path(path).absolute()
        # These two macOS system aliases are not user-controlled source links.
        # Do not resolve any other component; openat below still rejects them.
        if sys.platform == "darwin" and len(path.parts) > 1 and path.parts[1] in {"var", "tmp"}:
            alias = "/" + path.parts[1]
            try:
                target = os.readlink(alias)
            except OSError:
                target = None
            if target in {"private" + alias, "/private" + alias}:
                path = Path("/private") / Path(*path.parts[1:])
        if ".." in path.parts or len(path.parts) < 2:
            raise ValueError()
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory)
            os.close(directory)
            directory = next_fd
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError()
        return descriptor
    except (OSError, TypeError, ValueError):
        raise PdfPreflightError("invalid_source") from None
    finally:
        if directory is not None:
            os.close(directory)


def _source_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _snapshot(source, destination, limits, deadline, cancel_check):
    with os.fdopen(_open_source(source), "rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > limits.max_file_bytes:
            raise PdfPreflightError("file_limit")
        if before.st_size == 0:
            raise PdfPreflightError("corrupt_pdf")
        digest, count = hashlib.sha256(), 0
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as target:
            while True:
                _check(deadline, cancel_check)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > limits.max_file_bytes:
                    raise PdfPreflightError("file_limit")
                digest.update(block)
                target.write(block)
        if count != before.st_size or _source_identity(before) != _source_identity(os.fstat(stream.fileno())):
            raise PdfPreflightError("source_changed")
        return digest.hexdigest()


def _launch_worker(snapshot, result_path, limits):
    # Preserve the venv executable entry, not its resolved base interpreter.
    env = {"PATH": os.defpath, "HOME": str(snapshot.parent), "TMPDIR": str(snapshot.parent),
           "TMP": str(snapshot.parent), "TEMP": str(snapshot.parent), "LANG": "C", "LC_ALL": "C"}
    return subprocess.Popen(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker",
         str(snapshot), str(result_path), json.dumps(asdict(limits), separators=(",", ":"))],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=snapshot.parent, env=env, start_new_session=True,
    )


def _terminate_worker(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _flags(page, minimum):
    flags = []
    if page["nonspace_count"] == 0:
        flags.append("no_extractable_text")
    elif page["nonspace_count"] < minimum:
        flags.append("sparse_text")
    for field, flag in (("replacement_count", "replacement_characters"),
                        ("control_count", "control_characters"),
                        ("private_use_count", "private_use_characters")):
        if page[field]:
            flags.append(flag)
    if "parser_warnings" in page["flags"]:
        flags.append("parser_warnings")
    return flags


def _validate_result(value, digest, limits):
    def require(condition):
        if not condition:
            raise PdfPreflightError("invalid_result")
    require(type(value) is dict)
    if set(value) == {"error"}:
        require(type(value["error"]) is str and value["error"] in _MESSAGES)
        raise PdfPreflightError(value["error"])
    required = {"schema_version", "source_sha256", "parser_version", "status", "eligible_for_payment",
                "page_count", "total_char_count", "total_nonspace_count", "flags", "pages"}
    require(set(value) == required and value["schema_version"] == _SCHEMA
            and value["source_sha256"] == digest and value["parser_version"] == _PARSER_VERSION
            and value["eligible_for_payment"] is False)
    require(type(value["page_count"]) is int and 1 <= value["page_count"] <= limits.max_pages)
    require(type(value["pages"]) is list and len(value["pages"]) == value["page_count"])
    require(type(value["flags"]) is list
            and all(type(flag) is str and flag in _BOOK_FLAGS for flag in value["flags"]))
    require(value["flags"] == [flag for flag in _BOOK_FLAGS if flag in value["flags"]])
    require("memory_limit_unavailable" not in value["flags"] or sys.platform == "darwin")
    counts = ("char_count", "nonspace_count", "replacement_count", "control_count", "private_use_count")
    page_keys = {*counts, "page_number", "kind", "status", "image_xobject_count", "has_content_stream", "flags"}
    totals = {"char_count": 0, "nonspace_count": 0}
    for number, page in enumerate(value["pages"], 1):
        require(type(page) is dict and set(page) == page_keys)
        require(type(page["page_number"]) is int and page["page_number"] == number)
        for key in counts:
            require(type(page[key]) is int and 0 <= page[key] <= limits.max_page_chars)
        require(page["nonspace_count"] <= page["char_count"]
                and sum(page[key] for key in counts[2:]) <= page["nonspace_count"])
        require(type(page["image_xobject_count"]) is int and 0 <= page["image_xobject_count"] <= 10000
                and type(page["has_content_stream"]) is bool)
        require(type(page["flags"]) is list and all(type(flag) is str and flag in _PAGE_FLAGS for flag in page["flags"]))
        require(page["flags"] == _flags(page, limits.min_text_chars))
        require(page["kind"] == ("text" if page["nonspace_count"] else "empty_or_graphic"))
        require(page["status"] == ("review_required" if page["flags"] else "text_candidate"))
        for key in totals:
            totals[key] += page[key]
    for key, total in totals.items():
        require(type(value["total_" + key]) is int and value["total_" + key] == total
                and total <= limits.max_total_chars)
    review = bool(value["flags"]) or any(page["status"] == "review_required" for page in value["pages"])
    require(value["status"] == ("review_required" if review else "text_candidate"))
    return value


def _read_result(path, digest, limits):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PdfPreflightError("invalid_result")
            if info.st_size > limits.max_result_bytes:
                raise PdfPreflightError("result_limit")
            raw = stream.read(limits.max_result_bytes + 1)
        if len(raw) > limits.max_result_bytes:
            raise PdfPreflightError("result_limit")
        def object_pairs(pairs):
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError()
            return result
        value = json.loads(raw, object_pairs_hook=object_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return _validate_result(value, digest, limits)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise PdfPreflightError("invalid_result") from None


def inspect_text_pdf(path: Path, *, limits: PdfPreflightLimits | None = None, cancel_check=None) -> dict:
    """Inspect all pages locally; a candidate is never authority to bill/translate."""
    limits = _validate_limits(PdfPreflightLimits() if limits is None else limits)
    deadline = time.monotonic() + limits.timeout_seconds
    _check(deadline, cancel_check)
    with tempfile.TemporaryDirectory(prefix="pdf_text_preflight_") as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        snapshot, result = root / "source.pdf", root / "result.json"
        digest = _snapshot(path, snapshot, limits, deadline, cancel_check)
        _check(deadline, cancel_check)
        try:
            process = _launch_worker(snapshot, result, limits)
        except OSError:
            raise PdfPreflightError("worker_failed") from None
        try:
            while process.poll() is None:
                _check(deadline, cancel_check)
                try:
                    if result.lstat().st_size > limits.max_result_bytes:
                        raise PdfPreflightError("result_limit")
                except FileNotFoundError:
                    pass
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            _check(deadline, cancel_check)
            if process.returncode != 0:
                raise PdfPreflightError("worker_failed")
            return _read_result(result, digest, limits)
        finally:
            _terminate_worker(process)


def _child_sandbox(limits):
    # -I bypasses the repository test guard: enforce a separate no-network fence.
    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg"}:
            raise PermissionError("PDF preflight network disabled")
    sys.addaudithook(audit)
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(limits.timeout_seconds) + 1,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits.max_result_bytes,) * 2)
    memory_cap = (1024 * 1024 * 1024,) * 2
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, memory_cap)
        return True
    for limit in (resource.RLIMIT_AS, resource.RLIMIT_DATA):
        try:
            resource.setrlimit(limit, memory_cap)
            return True
        except (OSError, ValueError):
            pass
    # Darwin may reject both limits. This is visible as mandatory manual review,
    # never silently reported as memory-isolated / reliable / billable.
    return False


def _image_count(page):
    seen, count = set(), 0
    def visit(resources, depth):
        nonlocal count
        if depth > 20:
            raise PdfPreflightError("corrupt_pdf")
        if not resources:
            return
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        objects = resources.get("/XObject", {})
        objects = objects.get_object() if hasattr(objects, "get_object") else objects
        for reference in objects.values():
            key = (reference.idnum, reference.generation) if hasattr(reference, "idnum") else id(reference)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 10000:
                raise PdfPreflightError("corrupt_pdf")
            obj = reference.get_object()
            if obj.get("/Subtype") == "/Image":
                count += 1
            elif obj.get("/Subtype") == "/Form":
                visit(obj.get("/Resources", {}), depth + 1)
    visit(page.get("/Resources", {}), 0)
    return count


def _allow_empty_password_extraction(reader):
    """Respect explicit extraction permissions even if an empty owner key works."""
    from pypdf._encryption import PasswordType
    from pypdf.constants import UserAccessPermissions
    try:
        unlocked = reader.decrypt("")
    except Exception:
        raise PdfPreflightError("encrypted_pdf") from None
    if not isinstance(unlocked, PasswordType) or unlocked not in (
            PasswordType.USER_PASSWORD, PasswordType.OWNER_PASSWORD):
        raise PdfPreflightError("encrypted_pdf")
    try:
        encryption = reader.trailer["/Encrypt"].get_object()
        raw_permissions = encryption.get("/P")
        permissions = reader.user_access_permissions
        # pypdf normalizes /P modulo 2**32. Reject malformed values instead of
        # granting permission based on a truncated/rounded/defaulted bitmask.
        allowed = (isinstance(raw_permissions, int) and not isinstance(raw_permissions, bool)
                   and -(1 << 31) <= raw_permissions <= (1 << 32) - 1
                   and isinstance(permissions, UserAccessPermissions)
                   and int(permissions) == (raw_permissions & 0xffffffff)
                   and bool(permissions & UserAccessPermissions.EXTRACT))
    except Exception:
        allowed = False
    if not allowed:
        raise PdfPreflightError("extraction_forbidden")


def _parse_snapshot(source, limits):
    import importlib.metadata
    import logging
    import unicodedata
    import warnings
    try:
        if importlib.metadata.version("pypdf") != _PARSER_VERSION:
            raise PdfPreflightError("parser_unavailable")
        from pypdf import PdfReader
    except ImportError:
        raise PdfPreflightError("parser_unavailable") from None
    class WarningCounter(logging.Handler):
        count = 0
        def emit(self, record):
            self.count += 1  # Never retain or expose parser messages / source text.
    counter = WarningCounter(level=logging.WARNING)
    logger = logging.getLogger("pypdf")
    logger.handlers, logger.propagate, logger.level = [counter], False, logging.WARNING
    warnings.showwarning = lambda *args, **kwargs: setattr(counter, "count", counter.count + 1)
    warnings.simplefilter("always")
    reader = PdfReader(str(source), strict=True)
    encrypted = reader.is_encrypted
    if encrypted:
        _allow_empty_password_extraction(reader)
    count = len(reader.pages)
    if count == 0:
        raise PdfPreflightError("empty_pdf")
    if count > limits.max_pages:
        raise PdfPreflightError("page_limit")
    pages, total = [], 0
    for index, page in enumerate(reader.pages, 1):
        prior = counter.count
        text = page.extract_text()
        if type(text) is not str:
            raise PdfPreflightError("corrupt_pdf")
        total += len(text)
        if len(text) > limits.max_page_chars or total > limits.max_total_chars:
            raise PdfPreflightError("text_limit")
        statistics = {"page_number": index, "char_count": len(text),
                      "nonspace_count": sum(not ch.isspace() for ch in text),
                      "replacement_count": text.count("\ufffd"),
                      "control_count": sum(not ch.isspace() and unicodedata.category(ch) in {"Cc", "Cf"} for ch in text),
                      "private_use_count": sum(unicodedata.category(ch) == "Co" for ch in text),
                      "image_xobject_count": _image_count(page),
                      "has_content_stream": page.get("/Contents") is not None,
                      "flags": ["parser_warnings"] if counter.count > prior else []}
        statistics["flags"] = _flags(statistics, limits.min_text_chars)
        statistics["kind"] = "text" if statistics["nonspace_count"] else "empty_or_graphic"
        statistics["status"] = "review_required" if statistics["flags"] else "text_candidate"
        pages.append(statistics)
    flags = ["parser_warnings"] if counter.count else []
    if encrypted:
        flags.append("empty_password_encryption")
    return {"schema_version": _SCHEMA, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "parser_version": _PARSER_VERSION, "status": "review_required" if flags or any(p["flags"] for p in pages) else "text_candidate",
            "eligible_for_payment": False, "page_count": count, "total_char_count": total,
            "total_nonspace_count": sum(p["nonspace_count"] for p in pages), "flags": flags, "pages": pages}


def _worker_main(argv):
    if len(argv) != 4 or argv[0] != "--worker":
        return 2
    source, result = Path(argv[1]), Path(argv[2])
    try:
        limits = _validate_limits(PdfPreflightLimits(**json.loads(argv[3])))
        memory_limited = _child_sandbox(limits)
        try:
            payload = _parse_snapshot(source, limits)
            if not memory_limited:
                payload["flags"].append("memory_limit_unavailable")
                payload["status"] = "review_required"
        except PdfPreflightError as exc:
            payload = {"error": exc.reason}
        except Exception:
            payload = {"error": "corrupt_pdf"}
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(data) > limits.max_result_bytes:
            data = b'{"error":"result_limit"}'
        fd = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
