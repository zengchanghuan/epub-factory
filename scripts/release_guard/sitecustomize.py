"""Inherited Python-only offline guard; never an OS/network sandbox claim."""
import json
import os
import sys


if os.environ.get("RELEASE_GATE_ACTIVE") == "1":
    _settings = {key: os.environ.get(key, "") for key in (
        "RELEASE_GATE_ACTIVE", "RELEASE_GATE_GUARD_DIR", "RELEASE_GATE_EVENTS",
        "RELEASE_GATE_NETWORK_LOG", "RELEASE_GATE_DOTENV_ROOT")}

    def _record(path, value):
        if not path:
            raise RuntimeError("Release guard evidence destination missing")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(value, ensure_ascii=True) + "\n").encode())
        finally:
            os.close(fd)

    def _audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg",
                     "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo"}:
            # Never serialize addresses, URLs, provider payloads or credentials.
            _record(_settings["RELEASE_GATE_NETWORK_LOG"], {"event": event, "pid": os.getpid()})
            raise RuntimeError("Offline release gate forbids network: " + event)

    sys.addaudithook(_audit)
    _record(_settings["RELEASE_GATE_EVENTS"], {"kind": "guard_started", "pid": os.getpid()})

    import io
    from pathlib import Path
    import stat
    import dotenv
    import dotenv.main

    _original_dotenv = dotenv.load_dotenv
    _dotenv_root = Path(_settings["RELEASE_GATE_DOTENV_ROOT"])
    _dotenv_root_fd = None
    try:
        if not _dotenv_root.is_absolute() or ".." in _dotenv_root.parts:
            raise ValueError("Invalid isolated fixture root")
        _dotenv_root_fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
        for _component in _dotenv_root.parts[1:]:
            _next_fd = os.open(_component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=_dotenv_root_fd)
            os.close(_dotenv_root_fd)
            _dotenv_root_fd = _next_fd
    except (OSError, ValueError):
        if _dotenv_root_fd is not None:
            os.close(_dotenv_root_fd)
        _dotenv_root_fd = None

    def _load_fixture_dotenv(dotenv_path=None, stream=None, verbose=False,
                             override=False, interpolate=True, encoding="utf-8"):
        # Automatic discovery and caller streams can read outside the isolated
        # fixture tree. Capture its descriptor once; never trust mutable TMPDIR.
        if not dotenv_path or stream is not None or _dotenv_root_fd is None:
            return False
        descriptor = None
        try:
            candidate = Path(os.fsdecode(os.fspath(dotenv_path)))
            if ".." in candidate.parts:
                return False
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            relative = candidate.relative_to(_dotenv_root)
            if not relative.parts:
                return False
            descriptor = os.dup(_dotenv_root_fd)
            for component in relative.parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_fd
            leaf = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=descriptor)
            os.close(descriptor)
            descriptor = leaf
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return False
            # Parse the already-opened file to avoid a path check/open race.
            with io.open(descriptor, mode="r", encoding=encoding) as source:
                descriptor = None
                return _original_dotenv(stream=source, verbose=verbose, override=override,
                                        interpolate=interpolate, encoding=encoding)
        except (OSError, ValueError, TypeError):
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    dotenv.load_dotenv = _load_fixture_dotenv
    dotenv.main.load_dotenv = _load_fixture_dotenv

    import unittest
    _original_skip = unittest.result.TestResult.addSkip
    _original_run = unittest.TextTestRunner.run

    def _skip(self, test, reason):
        _record(_settings["RELEASE_GATE_EVENTS"], {
            "kind": "skip", "test_id": test.id(), "reason": str(reason), "pid": os.getpid()})
        return _original_skip(self, test, reason)

    def _run(self, test):
        result = _original_run(self, test)
        _record(_settings["RELEASE_GATE_EVENTS"], {
            "kind": "unittest_result", "tests_run": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "pid": os.getpid()})
        return result

    unittest.result.TestResult.addSkip = _skip
    unittest.TextTestRunner.run = _run

    def _inherit(environment):
        child = dict(os.environ if environment is None else environment)
        # Nested isolated self-tests may intentionally establish another guard
        # evidence directory. Preserve a complete explicit guard configuration.
        selected = ({key: child[key] for key in _settings}
                    if child.get("RELEASE_GATE_ACTIVE") == "1" and all(child.get(key) for key in _settings)
                    else _settings)
        child.update(selected)
        # Even an explicit nested guard configuration cannot enlarge the
        # parent's approved fixture root by changing TMPDIR or this variable.
        child["RELEASE_GATE_DOTENV_ROOT"] = _settings["RELEASE_GATE_DOTENV_ROOT"]
        guard = selected["RELEASE_GATE_GUARD_DIR"]
        paths = [part for part in str(child.get("PYTHONPATH", "")).split(os.pathsep) if part and part != guard]
        child["PYTHONPATH"] = os.pathsep.join([guard, *paths])
        return child

    def _check_flags(command):
        if not isinstance(command, (list, tuple)) or not command:
            return
        if not os.path.basename(os.fsdecode(command[0])).lower().startswith("python"):
            return
        for option in command[1:]:
            option = os.fsdecode(option)
            if option in {"-c", "-m"} or not option.startswith("-"):
                break
            if option.startswith("-") and not option.startswith("--") and any(flag in option[1:] for flag in "IES"):
                raise RuntimeError("Python isolation flags would disable the release guard")

    import subprocess
    _popen_init = subprocess.Popen.__init__

    def _popen(self, *args, **kwargs):
        _check_flags(args[0] if args else kwargs.get("args"))
        # Supplying env positionally makes safe inheritance ambiguous; current
        # repository calls use keyword arguments and fail closed otherwise.
        if len(args) > 10:
            raise RuntimeError("Release guard requires keyword subprocess environment")
        kwargs["env"] = _inherit(kwargs.get("env"))
        return _popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen

    import multiprocessing.util
    import threading
    _spawn = multiprocessing.util.spawnv_passfds
    _spawn_lock = threading.RLock()

    def _spawn_guarded(path, args, passfds):
        _check_flags([path, *args[1:]])
        # spawnv_passfds has no env argument. Preserve only the guard's required
        # variables across its synchronous fork/exec, then restore the caller.
        with _spawn_lock:
            inherited = _inherit(os.environ)
            keys = [*_settings, "PYTHONPATH"]
            previous = {key: os.environ.get(key) for key in keys}
            try:
                os.environ.update({key: inherited[key] for key in keys})
                return _spawn(path, args, passfds)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    multiprocessing.util.spawnv_passfds = _spawn_guarded
