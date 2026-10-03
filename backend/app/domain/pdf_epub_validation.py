"""Cancelable EPUBCheck boundary for the isolated PDF conversion workflow.

The shared validator alone interprets EPUBCheck JSON. This module owns only
process lifetime, transport budgets and a small, validated result envelope.
Python network auditing is not an OS sandbox for the trusted Java executable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.domain import pdf_text_preflight as guard

_RESULT_LIMIT = 8 * 1024
_JAVA_REPORT_LIMIT = 16 * 1024 * 1024
_ERRORS = {"EPUB_VALIDATION_FAILED", "EPUB_VALIDATION_UNAVAILABLE"}


@dataclass(frozen=True)
class PdfEpubValidationResult:
    passed: bool
    message: str
    error_code: str | None = None
    warnings: int = 0


def _unavailable():
    return PdfEpubValidationResult(False, "无法完成隔离 EPUB 校验，结果不可交付",
                                   "EPUB_VALIDATION_UNAVAILABLE")


def _read_result(path):
    def unique(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError()
        return value
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError()
        raw = stream.read(_RESULT_LIMIT + 1)
    if len(raw) > _RESULT_LIMIT:
        raise ValueError()
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if (type(value) is not dict or set(value) != {"passed", "message", "error_code", "warnings"}
            or type(value["passed"]) is not bool
            or type(value["message"]) is not str or not 0 < len(value["message"]) <= 512
            or any(ord(char) < 32 or ord(char) == 127 for char in value["message"])
            or type(value["warnings"]) is not int or not 0 <= value["warnings"] <= 1_000_000):
        raise ValueError()
    if value["passed"]:
        if value["error_code"] is not None:
            raise ValueError()
    elif type(value["error_code"]) is not str or value["error_code"] not in _ERRORS:
        raise ValueError()
    return PdfEpubValidationResult(**value)


def _java_executable():
    candidate = shutil.which("java")
    if candidate is None:
        return ""
    path = Path(candidate).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        return ""
    return str(path)


def _launch(epub, jar, result, java):
    root = result.parent
    search = str(Path(java).parent) + os.pathsep + os.defpath if java else os.defpath
    environment = {"PATH": search, "HOME": str(root), "TMPDIR": str(root),
                   "TMP": str(root), "TEMP": str(root), "LANG": "C", "LC_ALL": "C"}
    return subprocess.Popen(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker",
         str(epub), str(jar), str(result), java], cwd=root, env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def _terminate_group(process):
    # Java belongs to this group even if the Python leader exited unexpectedly.
    # Do not use a poll()-guard that leaves surviving grandchildren running.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def validate_pdf_epub(epub, jar, *, deadline, cancel_check=None):
    """Shared validation semantics with the caller's absolute monotonic deadline.

    Cancellation and elapsed deadlines raise PdfPreflightError. Other validator
    infrastructure failures return a non-deliverable result, just as the common
    EPUB gate does. No application settings or credentials enter the child.
    """
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise guard.PdfPreflightError("invalid_limits")
    check = lambda: guard._check(deadline, cancel_check)
    check()
    validation = _unavailable()
    try:
        epub, jar = Path(epub).absolute(), Path(jar).absolute()
        java = _java_executable()
        check()
        with tempfile.TemporaryDirectory(prefix="pdf_epubcheck_") as directory:
            root = Path(directory)
            root.chmod(0o700)
            report = root / "result.json"
            process = _launch(epub, jar, report, java)
            try:
                while process.poll() is None:
                    check()
                    try:
                        if report.lstat().st_size > _RESULT_LIMIT:
                            raise ValueError()
                    except FileNotFoundError:
                        pass
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
                check()
                if process.returncode == 0:
                    validation = _read_result(report)
                    check()
            finally:
                _terminate_group(process)
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        validation = _unavailable()
    check()
    return validation


def _load_shared_validator():
    # engine/__init__ imports compiler/configuration; load this one trusted
    # adjacent module directly. Its only app dependency is the inert models file.
    name = "_isolated_pdf_epub_validation_shared"
    path = Path(__file__).parents[1] / "engine" / "epub_validation.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _report_size(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_size > _JAVA_REPORT_LIMIT:
        raise OSError("EPUBCheck report exceeds safe transport limits")


def _bounded_java_run(java, command, *, capture_output, text, timeout):
    # The common validator remains responsible for every content/count/exit-code
    # decision. Only its subprocess transport is replaced; neither stdout nor
    # stderr participates in those decisions, so discard them instead of keeping
    # arbitrary Java output in Python memory.
    if (not isinstance(command, list) or len(command) != 6 or command[0] != "java"
            or command[1] != "-jar" or command[4] != "--json"
            or capture_output is not True or text is not True or timeout != 60):
        raise OSError("Unexpected EPUBCheck transport request")
    if not java:
        raise FileNotFoundError("Java unavailable")
    report = Path(command[-1])
    with subprocess.Popen([java, *command[1:]], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                _report_size(report)
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.025)
            _report_size(report)
            return subprocess.CompletedProcess(command, process.returncode, "", "")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def _worker(arguments):
    epub, jar, result, java = arguments
    os.umask(0o077)
    def audit(event, _args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg"}:
            raise PermissionError("PDF validation Python network disabled")
    sys.addaudithook(audit)
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_JAVA_REPORT_LIMIT, _JAVA_REPORT_LIMIT))
    try:
        shared = _load_shared_validator()
        shared.subprocess = SimpleNamespace(
            run=lambda *args, **kwargs: _bounded_java_run(java, *args, **kwargs),
            TimeoutExpired=subprocess.TimeoutExpired)
        validation = shared.validate_epub(epub, jar)
        value = {key: getattr(validation, key) for key in ("passed", "message", "error_code", "warnings")}
    except Exception:
        value = asdict(_unavailable())
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > _RESULT_LIMIT:
        encoded = json.dumps(asdict(_unavailable()), ensure_ascii=True).encode()
    descriptor = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    _worker(sys.argv[2:])
