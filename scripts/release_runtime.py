"""Local release preflight. No application imports, installation, or .env reads.

Only local version commands are executed. A temporary HOME and a restricted
environment prevent inherited application configuration from affecting them;
this is not a system-level sandbox or a supply-chain attestation.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile


COMMAND_TIMEOUT = 20
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9.!+_-]*\Z")
_NUM_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}\Z")
_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*)\])?==([0-9][A-Za-z0-9.!+_-]*)\Z")
_KEY_PACKAGES = ("lxml", "ebooklib", "beautifulsoup4", "opencc-python-reimplemented",
                 "mammoth", "mistune", "sqlalchemy", "celery", "billiard", "kombu",
                 "redis", "pydantic", "openai")

_PYTHON_PROBE = r'''
import importlib.metadata, json, re, socket, sqlite3, sys
def no_network(*args, **kwargs):
    raise RuntimeError("Version inspection forbids networking")
socket.socket.connect = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
from lxml import etree
packages = {}
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name", "")
    key = re.sub(r"[-_.]+", "-", name).lower()
    packages.setdefault(key, []).append(distribution.version)
print(json.dumps({
    "python": ".".join(map(str, sys.version_info[:3])),
    "python_full": sys.version,
    "sqlite": sqlite3.sqlite_version,
    "lxml": ".".join(map(str, etree.LXML_VERSION)),
    "libxml_compiled": ".".join(map(str, etree.LIBXML_COMPILED_VERSION)),
    "libxml_runtime": ".".join(map(str, etree.LIBXML_VERSION)),
    "libxslt_compiled": ".".join(map(str, etree.LIBXSLT_COMPILED_VERSION)),
    "libxslt_runtime": ".".join(map(str, etree.LIBXSLT_VERSION)),
    "packages": packages,
}, sort_keys=True))
'''


class _UnsafeFile(ValueError):
    pass


def _name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def _error(result, code, message):
    result["errors"].append({"code": code, "message": message})


def _read_beneath(root, relative, *, content=False):
    """Read via directory descriptors: no symlink component or traversal."""
    relative = str(relative)
    parts = relative.split("/")
    if (not relative or "\\" in relative or "\0" in relative or
            PurePosixPath(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts)):
        raise _UnsafeFile("unsafe")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    leaf = None
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            raise _UnsafeFile("unsafe")
        digest, chunks, size = hashlib.sha256(), [], 0
        while True:
            chunk = os.read(leaf, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if content:
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise _UnsafeFile("too_large")
                chunks.append(chunk)
        return (b"".join(chunks) if content else None), digest.hexdigest()
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(directory)


def _pins(raw):
    result = {}
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.fullmatch(line)
        if not match:
            raise ValueError(line_number)
        package, _, version = match.groups()
        package = _name(package)
        if package in result:
            raise ValueError(line_number)
        result[package] = version
    if not result:
        raise ValueError(0)
    return result


def _executable(root, value, search_path):
    if not isinstance(value, (str, os.PathLike)) or not str(value) or "\0" in str(value):
        raise ValueError()
    value = os.fspath(value)
    if "/" not in value:
        found = shutil.which(value, path=search_path)
        if not found:
            raise ValueError()
        path = Path(found)
    else:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
    path = Path(os.path.abspath(path))
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not os.access(path, os.X_OK):
        raise ValueError()
    # Keep the venv's symlink entry point: executing its target loses sys.prefix.
    return str(path), str(resolved)


def _command(argv, environment, cwd):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=COMMAND_TIMEOUT, check=False, env=environment, cwd=cwd)


def _python_versions(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("packages"), dict):
        raise ValueError()
    versions = {}
    for key in ("python", "sqlite", "lxml", "libxml_compiled", "libxml_runtime",
                "libxslt_compiled", "libxslt_runtime"):
        item = value.get(key)
        if not isinstance(item, str) or not _NUM_VERSION.fullmatch(item):
            raise ValueError()
        versions[key] = item
    # Read sys.version in the subprocess but do not publish arbitrary compiler
    # banners. Numeric runtime/library versions are the portable fingerprint.
    if not isinstance(value.get("python_full"), str):
        raise ValueError()
    installed = {}
    for package, candidates in value["packages"].items():
        if not isinstance(package, str) or not _NAME.fullmatch(package):
            raise ValueError()
        if not isinstance(candidates, list) or len(candidates) != 1:
            raise ValueError()
        version = candidates[0]
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise ValueError()
        normalized = _name(package)
        if normalized in installed:
            raise ValueError()
        installed[normalized] = version
    return versions, installed


def inspect_runtime(root: Path, python: str, node: str, jar: Path) -> dict:
    """Require exact lock pins, Python 3.10.12, Node 22, Java 17/EPUBCheck 5.1.0."""
    result = {"ok": False, "errors": [], "versions": {}, "executables": {},
              "lock_sha256": None, "jar_sha256": None, "requirements_drift": [],
              "key_packages": {}, "extra_packages": {}, "packages_sha256": None}
    try:
        root = Path(root).resolve(strict=True)
        raw, result["lock_sha256"] = _read_beneath(root, "backend/requirements.lock", content=True)
        pins = _pins(raw)
    except (OSError, ValueError, UnicodeError):
        _error(result, "requirements_lock_invalid", "The dependency lock is missing, unsafe, or not a plain exact-pin lock.")
        pins = None
    try:
        jar_path = Path(jar).expanduser()
        if not jar_path.is_absolute():
            jar_path = root / jar_path
        jar_path = jar_path.resolve(strict=True)
        _, result["jar_sha256"] = _read_beneath(jar_path.parent, jar_path.name)
    except (OSError, ValueError, TypeError):
        _error(result, "epubcheck_jar_invalid", "The explicit EPUBCheck JAR is missing or is not a regular local file.")
        jar_path = None
    search_path = os.environ.get("PATH", os.defpath)
    commands = {}
    for label, value in (("python", python), ("node", node), ("java", "java")):
        try:
            commands[label], resolved = _executable(root, value, search_path)
            result["executables"][label] = {"path": commands[label], "resolved_path": resolved}
        except (OSError, ValueError, TypeError):
            _error(result, label + "_unavailable", "The selected " + label + " executable is unavailable.")
    with tempfile.TemporaryDirectory(prefix="fixepub-preflight-home-") as home:
        environment = {"PATH": search_path, "HOME": home, "TMPDIR": home,
                       "LANG": "C", "LC_ALL": "C", "TZ": "UTC"}
        for label, args in (("python", ["-I", "-B", "-c", _PYTHON_PROBE]),
                            ("node", ["--version"]), ("java", ["-version"]),
                            ("epubcheck", ["-jar", str(jar_path), "--version"])):
            executable = commands.get("java" if label == "epubcheck" else label)
            if not executable or (label == "epubcheck" and jar_path is None):
                continue
            try:
                reply = _command([executable, *args], environment, home)
                if reply.returncode:
                    raise ValueError()
                output = (reply.stdout or "") + "\n" + (reply.stderr or "")
                if label == "python":
                    versions, installed = _python_versions(reply.stdout)
                    result["versions"].update(versions)
                    result["key_packages"] = {key: installed.get(key) for key in _KEY_PACKAGES}
                    result["packages_sha256"] = hashlib.sha256(json.dumps(installed, sort_keys=True).encode()).hexdigest()
                    if pins is not None:
                        result["requirements_drift"] = [{"package": key, "expected": version, "installed": installed.get(key)}
                            for key, version in sorted(pins.items()) if installed.get(key) != version]
                        result["extra_packages"] = {key: version for key, version in sorted(installed.items()) if key not in pins}
                        if result["requirements_drift"]:
                            _error(result, "requirements_drift", "Installed packages differ from the exact dependency lock.")
                    if versions["python"] != "3.10.12":
                        _error(result, "python_version", "Python 3.10.12 is required.")
                elif label == "node":
                    match = re.fullmatch(r"v([0-9]+\.[0-9]+\.[0-9]+)\s*", (reply.stdout or "").strip())
                    if not match: raise ValueError()
                    result["versions"][label] = match[1]
                    if match[1].split(".")[0] != "22":
                        _error(result, "node_version", "Node.js major version 22 is required.")
                elif label == "java":
                    match = re.search(r'(?:openjdk|java) version "([0-9]+(?:\.[0-9]+){0,3})(?:[_+\-][0-9A-Za-z.-]+)?"', output)
                    if not match: raise ValueError()
                    result["versions"][label] = match[1]
                    if match[1].split(".")[0] != "17":
                        _error(result, "java_version", "Java major version 17 is required.")
                else:
                    lines = [line.strip() for line in output.splitlines() if line.strip()]
                    match = (re.fullmatch(r"EPUBCheck v?([0-9]+\.[0-9]+\.[0-9]+)", lines[0])
                             if lines else None)
                    if not match or lines[1:] not in ([], [
                            "Messages: 0 fatals / 0 errors / 0 warnings / 0 infos", "EPUBCheck completed"]):
                        raise ValueError()
                    result["versions"][label] = match[1]
                    if match[1] != "5.1.0":
                        _error(result, "epubcheck_version", "EPUBCheck 5.1.0 is required.")
            except subprocess.TimeoutExpired:
                _error(result, label + "_timeout", "Local " + label + " version inspection timed out.")
            except (OSError, ValueError, TypeError, KeyError):
                _error(result, label + "_probe_failed", "Local " + label + " version inspection failed or returned an invalid response.")
    result["ok"] = not result["errors"]
    return result


def _literal(root, relative, name):
    raw, _ = _read_beneath(root, relative, content=True)
    tree = ast.parse(raw.decode("utf-8"))
    matches = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            matches.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            matches.append(node.value)
    if len(matches) != 1:
        raise ValueError()
    return ast.literal_eval(matches[0])


def inspect_books(root, uploads, outputs, baselines) -> dict:
    """Hash all nine pinned fixtures; never imports a test or reads EPUB text."""
    result = {"ok": False, "errors": [], "files": []}
    try:
        root = Path(root).resolve(strict=True)
        books = _literal(root, "backend/test_d37_entitlement_history.py", "BOOKS")
        expected_baselines = _literal(root, "backend/test_d54_infra_history.py", "BASELINE_SHA256")
        if not isinstance(books, (list, tuple)) or len(books) != 3 or not isinstance(expected_baselines, dict):
            raise ValueError()
        keys = []
        for book in books:
            if not isinstance(book, dict) or not isinstance(book.get("key"), str) or not re.fullmatch(r"[a-z0-9-]+", book["key"]):
                raise ValueError()
            keys.append(book["key"])
            for role in ("input", "output"):
                if not isinstance(book.get(role), str) or not isinstance(book.get(role + "_sha256"), str) or not _SHA.fullmatch(book[role + "_sha256"]):
                    raise ValueError()
            if not isinstance(expected_baselines.get(book["key"]), str) or not _SHA.fullmatch(expected_baselines[book["key"]]):
                raise ValueError()
        if len(set(keys)) != 3 or set(keys) != set(expected_baselines):
            raise ValueError()
    except (OSError, ValueError, TypeError, SyntaxError, UnicodeError):
        _error(result, "history_manifest_invalid", "Historical fixture pins must be valid literal three-book manifests.")
        return result
    directories = {}
    for role, directory in (("input", uploads), ("output", outputs), ("baseline", baselines)):
        try:
            directories[role] = Path(directory).expanduser().resolve(strict=True)
            if not directories[role].is_dir():
                raise ValueError()
        except (OSError, ValueError, TypeError):
            _error(result, role + "_directory_invalid", "An explicit existing " + role + " fixture directory is required.")
    for book in books:
        for role in ("input", "output", "baseline"):
            relative = book[role] if role != "baseline" else book["input_sha256"][:12] + "/converted.epub"
            expected = book[role + "_sha256"] if role != "baseline" else expected_baselines[book["key"]]
            directory = directories.get(role)
            record = {"role": role, "book": book["key"], "path": str(directory / relative) if directory else None,
                      "sha256": None, "expected_sha256": expected}
            result["files"].append(record)
            if directory is None:
                continue
            try:
                _, record["sha256"] = _read_beneath(directory, relative)
                if record["sha256"] != expected:
                    _error(result, "history_hash_mismatch", "The " + role + " fixture hash differs for " + book["key"] + ".")
            except FileNotFoundError:
                _error(result, "history_file_missing", "The " + role + " fixture is missing for " + book["key"] + ".")
            except (OSError, ValueError):
                _error(result, "history_file_unsafe", "The " + role + " fixture is unsafe or unreadable for " + book["key"] + ".")
    result["ok"] = not result["errors"] and len(result["files"]) == 9
    return result
