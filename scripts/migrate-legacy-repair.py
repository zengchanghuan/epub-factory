#!/usr/bin/env python3
"""One-time preservation of one verified, non-test legacy repair. Preview by default.

Run with the production virtualenv Python. This tool never starts/stops services,
queries the payment gateway, changes payment state, or overwrites order.json.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid


LEGACY_MAIN_SHA256 = "f5d3153ea7cdf044d472fb84fec326714c5b28ede241fdf36820f33b6f38aed5"
KEYS = ("REPAIR_PRICE_CNY", "REPAIR_UPLOAD_DIR", "SKIP_PAYMENT_CHECK")
JOB_ID = re.compile(r"[0-9a-f]{32}\Z")


class Refused(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def file_hash(path):
    require(stat.S_ISREG(path.lstat().st_mode), "Expected a regular, non-symlink file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
    require(result.returncode == 0, "Required read-only service inspection failed")
    return result.stdout.strip()


def check_quiet(_proc_root=Path("/proc"), _drain_seconds=3):
    require(command("systemctl", "show", "nginx", "-p", "ActiveState", "--value") == "inactive",
            "nginx must already be inactive; this tool does not stop it")
    result = subprocess.run(("pgrep", "-x", "nginx"), capture_output=True, timeout=10, check=False)
    require(result.returncode == 1, "nginx processes remain, or process inspection failed")
    # Reject established/in-flight connections, including non-loopback bypasses.
    deadline = time.monotonic() + _drain_seconds
    while True:
        listeners, active = [], False
        for name in ("tcp", "tcp6"):
            for line in (_proc_root / "net" / name).read_text().splitlines()[1:]:
                fields = line.split()
                address, port = fields[1].split(":")
                if int(port, 16) != 8000:
                    continue
                if fields[3] == "0A":
                    require(name == "tcp" and address == "0100007F", "API listener is not exclusively 127.0.0.1:8000")
                    listeners.append(fields[9])
                elif fields[3] != "06":
                    active = True
        require(len(listeners) == 1, "Expected exactly one loopback API listener")
        if not active:
            return listeners[0]
        require(time.monotonic() < deadline, "API still has an active/in-flight TCP connection")
        time.sleep(0.1)


def read_server(project, apply, confirmed_price_unchanged=False, _proc_root=Path("/proc")):
    from dotenv import dotenv_values

    main = project / "backend/app/main.py"
    require(file_hash(main) == LEGACY_MAIN_SHA256, "Production main.py differs from the reviewed legacy version")
    pid_text = command("systemctl", "show", "epub-factory", "-p", "MainPID", "--value")
    require(pid_text.isdecimal() and int(pid_text) > 1, "Cannot identify the running legacy API")
    proc = _proc_root / pid_text
    argv = [part.decode() for part in (proc / "cmdline").read_bytes().split(b"\0") if part]
    uvicorn = any(Path(arg).name == "uvicorn" for arg in argv[:2]) or argv[1:3] == ["-m", "uvicorn"]
    require(uvicorn and "app.main:app" in argv, "Unexpected API command")
    for flag, value in (("--host", "127.0.0.1"), ("--port", "8000")):
        require(flag in argv and argv[argv.index(flag) + 1:argv.index(flag) + 2] == [value],
                "API must explicitly bind 127.0.0.1:8000")
    require("--reload" not in argv, "Reloading API cannot be migrated safely")
    require("--workers" not in argv or argv[argv.index("--workers") + 1:argv.index("--workers") + 2] == ["1"],
            "Multiple API workers cannot be migrated safely")
    # A parent holding a shared socket could hide worker-local order dictionaries.
    children = (proc / "task" / pid_text / "children").read_text().strip()
    require(not children, "API has child processes; single-worker state cannot be established")
    inherited = dict(part.split(b"=", 1) for part in (proc / "environ").read_bytes().split(b"\0") if b"=" in part)
    inherited = {key: inherited.get(key.encode(), None) for key in KEYS}
    inherited = {key: value.decode() if value is not None else None for key, value in inherited.items()}
    config = dotenv_values(project / "backend/.env", interpolate=False)
    configured = {key: config.get(key) for key in KEYS}
    effective = {key: inherited[key] if inherited[key] is not None else configured[key] for key in KEYS}
    require(all("${" not in (value or "") for value in effective.values()), "Interpolated runtime configuration is not supported")
    require((effective["REPAIR_PRICE_CNY"] or "5.99").strip() in ("", "5.99"), "Effective old repair price is not 5.99")
    require((effective["SKIP_PAYMENT_CHECK"] or "").lower() not in ("1", "true", "yes"), "Payment checks are disabled")
    btime = next(int(line.split()[1]) for line in (_proc_root / "stat").read_text().splitlines() if line.startswith("btime "))
    ticks = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[19])
    started = btime + ticks / os.sysconf("SC_CLK_TCK")
    require(main.stat().st_mtime <= started, "Code changed after API startup; loaded legacy version is unproven")
    config_changed = (project / "backend/.env").stat().st_mtime > started
    require(not config_changed or confirmed_price_unchanged,
            "Config changed after startup; --confirmed-price-unchanged-since-start is required for the reviewed SMTP-only change")
    directory = Path(effective["REPAIR_UPLOAD_DIR"] if effective["REPAIR_UPLOAD_DIR"] is not None else "/tmp/epub-repair")
    require(directory.is_absolute() and directory.is_dir() and not directory.is_symlink(), "Unsafe or missing repair directory")
    listener = check_quiet() if apply else None
    if listener:
        sockets = set()
        for fd in (proc / "fd").iterdir():
            try:
                sockets.add(os.readlink(fd))
            except FileNotFoundError:
                continue
        require(f"socket:[{listener}]" in sockets, "API MainPID does not own the inspected listener")
    return directory, {"api_pid": int(pid_text), "api_started": started,
                       "main_sha256": LEGACY_MAIN_SHA256, "inherited": inherited,
                       "dotenv": configured, "effective_old_price": "5.99",
                       "config_changed_after_start": config_changed,
                       "price_unchanged_since_start_confirmation": confirmed_price_unchanged,
                       "configuration_evidence": "Reviewed SMTP-only configuration change in the authorizing deployment session."
                       if config_changed else "Configuration predates API startup."}


def get_status(job_id):
    request = Request(f"http://127.0.0.1:8000/api/v2/repair/{job_id}/status", headers={"Connection": "close"})
    try:
        with urlopen(request, timeout=10) as response:
            require(response.status == 200, "Unexpected status response")
            result = json.loads(response.read(65537))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise Refused("Status request failed") from None
    require(isinstance(result, dict) and result.get("job_id") == job_id, "Malformed status response")
    return result


def inspect_orders(directory, job_id):
    require(JOB_ID.fullmatch(job_id), "An explicit 32-character lowercase repair id is required")
    for entry in directory.iterdir():
        require(not entry.is_symlink(), "Symlink in repair directory")
        if not entry.is_dir():
            continue
        require(JOB_ID.fullmatch(entry.name), "Unexpected repair directory; inventory requires manual review")
        metadata = entry / "order.json"
        if metadata.exists() or metadata.is_symlink():
            require(entry.name != job_id, "Target already has order.json; refusing overwrite")
            continue
        if entry.name != job_id:
            require(get_status(entry.name) is None, "Another live memory-only repair exists; refusing partial migration")
    job_dir = directory / job_id
    require(job_dir.is_dir() and not job_dir.is_symlink(), "Target repair directory is missing or unsafe")
    require(not list(job_dir.glob("*_fixed.epub")), "Existing repair output requires separate review")
    sources = [p for p in job_dir.glob("*.epub") if "_fixed" not in p.stem]
    require(len(sources) == 1, "Expected exactly one original EPUB")
    source = sources[0]
    return source, file_hash(source)


def create_private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def preserve(project, directory, job_id, evidence, apply):
    source, digest = inspect_orders(directory, job_id)
    if apply:
        check_quiet()
    status = get_status(job_id)  # Last API read: never infer payment from gateway errors.
    require(status is not None and status.get("status") == "pending_payment", "Final live state must be pending_payment")
    require(not status.get("error") and not status.get("download_filename"), "Unexpected terminal details require review")
    metadata = {"status": "pending_payment", "filename": source.name,
                "quoted_amount": "5.99", "expected_amount": "5.99",
                "out_trade_no": "repair_" + job_id, "is_test_order": False}
    snapshot = {"version": 1, "job_id": job_id, "source_sha256": digest,
                "source_bytes": source.stat().st_size, "status_response": status,
                "evidence": evidence, "metadata": metadata,
                "payment_note": "Local pending status only; gateway payment state is unknown.",
                "created_at_epoch": time.time()}
    if not apply:
        return {"mode": "preview", "job_id": job_id, "status": "pending_payment",
                "expected_amount": "5.99", "source_sha256": digest,
                "checks": "Reviewed legacy code, runtime price, single API process, live inventory, pending state and source hash.",
                "next_step": "Stop nginx and drain connections outside this tool, then use --apply."}
    require(file_hash(source) == digest, "Source changed during inspection")
    backups = project / "deploy-backups"
    require(not backups.is_symlink(), "Unsafe backup directory")
    backups.mkdir(mode=0o700, exist_ok=True)
    private = backups / ("legacy-repair-" + job_id + "-" + uuid.uuid4().hex)
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    create_private_json(private / "snapshot.json", snapshot)
    # Hard-link a fully flushed private file into place: atomic and no overwrite.
    temporary = source.parent / (".migration-" + uuid.uuid4().hex + ".tmp")
    try:
        create_private_json(temporary, metadata)
        os.link(temporary, source.parent / "order.json")
        directory_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    require(file_hash(source) == digest, "Source hash changed; preserve all migration evidence")
    return {"mode": "applied", "job_id": job_id, "status": "pending_payment",
            "expected_amount": "5.99", "source_sha256": digest,
            "snapshot_path": str(private / "snapshot.json"),
            "rollback_note": "Preserve order.json and source; legacy code cannot reload this metadata."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--confirmed-nontest", action="store_true")
    parser.add_argument("--confirmed-price-unchanged-since-start", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        require(args.confirmed_nontest, "Explicit --confirmed-nontest is required for this known user order")
        require(JOB_ID.fullmatch(args.job_id), "Invalid job id")
        project = args.project_dir.resolve(strict=True)
        directory, evidence = read_server(project, args.apply, args.confirmed_price_unchanged_since_start)
        result = preserve(project, directory, args.job_id, evidence, args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (Refused, OSError, ValueError, ImportError, subprocess.SubprocessError):
        # Do not print raw subprocess/network/environment exceptions with secrets.
        import sys
        exc = sys.exc_info()[1]
        reason = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        print(json.dumps({"mode": "refused", "reason": reason}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
