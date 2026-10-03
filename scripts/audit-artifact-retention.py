#!/usr/bin/env python3
"""Offline, read-only retention inventory; NEVER authorizes deletion.

Supply ONE consistent SQLite backup (no WAL/SHM/journal siblings), not a live
database. All four artifact roots must be explicit. No application imports,
schema migration, environment/config discovery, network, TTL or cleanup.
Books are hashed as opaque bytes; their text is never parsed or reported.
"""
import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys


SCHEMA = "artifact-retention-audit-v1"
TABLES = {
    "epub_jobs": ("id", "input_path", "output_path", "status", "batch_id",
                  "translation_stats_json", "payment_entitlement_json", "payment_resolution_json"),
    "job_dispatch_outbox": ("job_id", "attempt_id", "status"),
    "job_executions": ("job_id", "attempt_id", "owner", "state"),
    "admin_order_reviews": ("order_no", "state"),
    "admin_order_review_events": ("order_no", "result_json"),
    "order_funnel_events": ("order_no", "event"),
}
KNOWN_TABLES = set(TABLES) | {
    "users", "job_chapters", "job_chunks", "job_stages", "notifications",
    "job_email_subscriptions", "payment_email_outbox", "admin_order_review_events",
    "llm_preflight_budgets", "llm_preflight_cache", "llm_usage_attempts", "llm_usage_requests",
}
ACTIVE = {"awaiting_confirm", "confirming", "pending_payment", "pending", "running"}
STATUSES = ACTIVE | {"success", "failed", "cancelled"}
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class AuditError(ValueError):
    """Only a fixed reason code leaves the process; never raw input/errors."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Limits:
    max_entries: int = 100_000
    max_rows: int = 100_000
    max_file_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 8 * 1024 * 1024 * 1024
    max_metadata_bytes: int = 4 * 1024 * 1024
    max_depth: int = 32

    def validate(self):
        for name, cap in (("max_entries", 100_000), ("max_rows", 100_000),
                          ("max_file_bytes", 256 * 1024 * 1024),
                          ("max_total_bytes", 8 * 1024 * 1024 * 1024),
                          ("max_metadata_bytes", 4 * 1024 * 1024), ("max_depth", 64)):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= cap:
                raise AuditError("invalid_limits")


def _alias(path):
    """Only macOS's standard filesystem aliases are canonicalized."""
    value = Path(path)
    if ".." in value.parts:
        raise AuditError("unsafe_path")
    if not value.is_absolute():
        value = Path.cwd() / value
    if sys.platform == "darwin":
        for alias, target in (("/tmp", "/private/tmp"), ("/var", "/private/var")):
            if (str(value) == alias or str(value).startswith(alias + "/")) and os.path.islink(alias):
                if os.readlink(alias) != target and os.readlink(alias) != target.lstrip("/"):
                    raise AuditError("unsafe_path")
                value = Path(target + str(value)[len(alias):])
    return value


def _open(path, *, directory=False):
    value = _alias(path)
    if value == Path("/"):
        raise AuditError("unsafe_path")
    descriptor = os.open("/", _DIR)
    try:
        for part in value.parts[1:-1]:
            following = os.open(part, _DIR, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        result = os.open(value.name, _DIR if directory else _FILE, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    return result


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _fingerprint_fd(descriptor, limit):
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise AuditError("unsafe_entry")
    if before.st_size > limit:
        raise AuditError("file_limit")
    digest, size = hashlib.sha256(), 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise AuditError("file_limit")
        digest.update(chunk)
    if _identity(before) != _identity(os.fstat(descriptor)) or size != before.st_size:
        raise AuditError("source_changed")
    return _identity(before), digest.hexdigest()


def _fingerprint(path, limit):
    descriptor = _open(path)
    try:
        return _fingerprint_fd(descriptor, limit)
    finally:
        os.close(descriptor)


def _infrastructure(name):
    lower = name.lower()
    return (lower.startswith(".env") or lower.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm",
            "-journal", ".lock", ".pem", ".key", ".p12", ".pfx"))
            or lower in {".repair-executor.json", ".repair-gateway.json"})


def _scan(roots, limits):
    entries, byte_count = {}, 0
    def walk(label, descriptor, prefix, device, depth):
        nonlocal byte_count
        if depth > limits.max_depth:
            raise AuditError("depth_limit")
        directory_before = _identity(os.fstat(descriptor))
        with os.scandir(descriptor) as listing:
            names = []
            for item in listing:
                names.append(item.name)
                if len(names) + len(entries) > limits.max_entries:
                    raise AuditError("entry_limit")
        for name in sorted(names):
            if len(name.encode("utf-8", "strict")) > 512:
                raise AuditError("unsafe_entry")
            relative = prefix + name
            value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if value.st_dev != device or not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
                raise AuditError("unsafe_entry")
            if len(entries) >= limits.max_entries:
                raise AuditError("entry_limit")
            entry = {"root": label, "relative_path": relative, "identity": _identity(value),
                     "kind": "directory" if stat.S_ISDIR(value.st_mode) else "file", "sha256": None}
            entries[(label, relative)] = entry
            if entry["kind"] == "directory":
                child = os.open(name, _DIR, dir_fd=descriptor)
                try:
                    if _identity(os.fstat(child)) != entry["identity"]:
                        raise AuditError("source_changed")
                    walk(label, child, relative + "/", device, depth + 1)
                finally:
                    os.close(child)
            elif not _infrastructure(name):
                byte_count += value.st_size
                if byte_count > limits.max_total_bytes:
                    raise AuditError("total_byte_limit")
                child = os.open(name, _FILE, dir_fd=descriptor)
                try:
                    identity, digest = _fingerprint_fd(child, limits.max_file_bytes)
                    if identity != entry["identity"]:
                        raise AuditError("source_changed")
                    entry["sha256"] = digest
                finally:
                    os.close(child)
        if directory_before != _identity(os.fstat(descriptor)):
            raise AuditError("source_changed")
        return directory_before
    root_identities = {}
    for label, root in roots.items():
        descriptor = _open(root, directory=True)
        try:
            root_identities[label] = walk(label, descriptor, "", os.fstat(descriptor).st_dev, 0)
        finally:
            os.close(descriptor)
    return entries, root_identities


def _backup_only(database):
    if any(os.path.lexists(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        raise AuditError("database_sidecar_present")


def _read_database(database, descriptor, limits):
    _backup_only(database)
    # immutable is ONLY valid because an explicit offline consistent backup is
    # required, sidecars are forbidden, and the file is verified before/after.
    # Pin the inode, not a pathname an independent writer could replace and
    # restore between our two scans. Never fall back to reopening database.
    if sys.platform == "darwin":
        pinned = Path("/dev/fd") / str(descriptor)
    elif sys.platform.startswith("linux"):
        pinned = Path("/proc/self/fd") / str(descriptor)
    else:
        raise AuditError("unsupported_platform")
    with closing(sqlite3.connect(pinned.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise AuditError("invalid_database")
        objects = dict(connection.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        if any(name not in KNOWN_TABLES for name, kind in objects.items() if kind == "table"):
            raise AuditError("unsupported_schema")
        rows, count = {}, 0
        for table, columns in TABLES.items():
            if objects.get(table) != "table":
                raise AuditError("unsupported_schema")
            actual = {row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')}
            if not set(columns) <= actual:
                raise AuditError("unsupported_schema")
            rows[table] = []
            for values in connection.execute('SELECT ' + ','.join('"' + col + '"' for col in columns) + ' FROM "' + table + '"'):
                count += 1
                if count > limits.max_rows:
                    raise AuditError("row_limit")
                if any(isinstance(value, str) and len(value.encode()) > limits.max_metadata_bytes for value in values):
                    raise AuditError("metadata_limit")
                rows[table].append(dict(zip(columns, values)))
        return rows


def _opaque(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]


def _json(value, limit):
    if value in (None, ""):
        return {}
    if not isinstance(value, str) or len(value.encode()) > limit:
        raise ValueError()
    def pairs(items):
        answer = {}
        for key, item in items:
            if key in answer:
                raise ValueError()
            answer[key] = item
        return answer
    result = json.loads(value, object_pairs_hook=pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if type(result) is not dict:
        raise ValueError()
    return result


def _classify(rows, roots, entries, limits):
    references, prefixes, missing, issues = {}, {}, [], []
    active, uncertain = False, False
    jobs = {row["id"]: row for row in rows["epub_jobs"] if isinstance(row["id"], str)}
    reasons = {key: set() for key in jobs}
    order_jobs = {}
    for key, job in jobs.items():
        order_jobs.setdefault(key, set()).add(key)
        if isinstance(job["batch_id"], str) and job["batch_id"]:
            order_jobs.setdefault("batch_" + job["batch_id"], set()).add(key)
    if len(jobs) != len(rows["epub_jobs"]):
        uncertain = True
        issues.append({"reason": "invalid_job_identity"})

    def bind(key, owner, reason, *, directory=False, required=True):
        target = prefixes if directory else references
        owners, why = target.setdefault(key, (set(), set()))
        owners.add(_opaque(owner)); why.add(reason)
        if required and key not in entries:
            missing.append({"root": key[0], "relative_path": key[1],
                            "owner": _opaque(owner), "reason": reason})

    def reference(path, owner, reason):
        nonlocal uncertain
        if not path:
            return
        if not isinstance(path, str) or not Path(path).is_absolute():
            uncertain = True
            issues.append({"owner": _opaque(owner), "reason": "unresolved_reference"})
            return
        try:
            normalized = _alias(path)
            for label, root in roots.items():
                if normalized != root and normalized.is_relative_to(root):
                    bind((label, normalized.relative_to(root).as_posix()), owner, reason)
                    return
        except (ValueError, OSError):
            pass
        uncertain = True
        issues.append({"owner": _opaque(owner), "reason": "reference_outside_roots"})

    for row in rows["job_dispatch_outbox"] + rows["job_executions"]:
        key = row["job_id"]
        if key not in jobs:
            uncertain = True
            issues.append({"owner": _opaque(key), "reason": "unresolved_execution_owner"})
            continue
        state = row.get("status", row.get("state"))
        if state in {"pending", "publishing", "sent", "running", "queued"} and jobs[key]["status"] in ACTIVE:
            reasons[key].add("active_dispatch_or_execution"); active = True
        if state not in {"pending", "publishing", "sent", "obsolete", "running", "queued", "finished", "exhausted"}:
            uncertain = True
            reasons[key].add("unknown_execution_metadata")
    for row in rows["admin_order_reviews"]:
        if row["state"] not in {"open", "closed"}:
            uncertain = True
        if row["state"] == "open":
            for key in order_jobs.get(row["order_no"], ()):
                reasons[key].add("open_manual_review")
            if row["order_no"] not in order_jobs:
                uncertain = True
                issues.append({"owner": _opaque(row["order_no"]), "reason": "unresolved_review_owner"})
    for row in rows["order_funnel_events"]:
        if row["event"] == "payment_succeeded":
            for key in order_jobs.get(row["order_no"], ()):
                reasons[key].add("recorded_payment_event")
    for row in rows["admin_order_review_events"]:
        try:
            prior = _json(row["result_json"], limits.max_metadata_bytes).get("prior_artifacts", {})
            if type(prior) is not dict:
                raise ValueError()
            for owner, path in prior.items():
                if not isinstance(owner, str) or not isinstance(path, str) or not path:
                    raise ValueError()
                reference(path, owner, "prior_review_artifact")
        except (TypeError, ValueError):
            uncertain = True
            issues.append({"owner": _opaque(row["order_no"]), "reason": "invalid_review_metadata"})
    for key, job in jobs.items():
        state = job["status"]
        if state in ACTIVE:
            active = True; reasons[key].add("active_or_unsettled_job")
        elif state not in STATUSES:
            uncertain = True; reasons[key].add("unknown_job_status")
        if state == "success":
            reasons[key].add("completed_output_retained")
        if not job["input_path"] or (state == "success" and not job["output_path"]):
            uncertain = True
            reasons[key].add("missing_required_file_pointer")
            issues.append({"owner": _opaque(key), "reason": "missing_required_file_pointer"})
        try:
            stats = _json(job["translation_stats_json"], limits.max_metadata_bytes)
            payment = _json(job["payment_resolution_json"], limits.max_metadata_bytes)
            entitlement = _json(job["payment_entitlement_json"], limits.max_metadata_bytes)
            if ((payment and payment.get("state") not in {"paid", "paid_review", "closed", "external_refund_recorded"})
                    or (entitlement and entitlement.get("state") not in {"quoted", "paid", "test_authorized"})):
                raise ValueError()
            if payment.get("state") in {"paid", "paid_review", "external_refund_recorded"} or entitlement.get("state") in {"paid", "test_authorized"}:
                reasons[key].add("payment_or_review_record_retained")
            pdf = stats.get("pdf_conversion")
            if pdf is not None:
                if (type(pdf) is not dict or type(pdf.get("schema_version")) is not int or pdf.get("schema_version") != 1
                        or pdf.get("product") != "pdf_text_conversion"
                        or pdf.get("phase") not in {"preparing", "prepared", "confirmed"}):
                    raise ValueError()
                if pdf["phase"] != "preparing":
                    identity = pdf.get("artifact_id")
                    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
                        raise ValueError()
                    directory = ".pdf-prepared-" + identity
                    bind(("outputs", directory), key, "pdf_prepared_plan", directory=True)
                    bind(("outputs", directory + "/book.epub"), key, "pdf_prepared_artifact")
            # Hash directory mapping avoids reading chapter JSON bodies.
            prefix = "v2/" + hashlib.sha256(key.encode("ascii")).hexdigest()
            bind(("reduce_work", prefix), key, "known_job_checkpoints", directory=True, required=False)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            uncertain = True; reasons[key].add("invalid_job_metadata")
        reference(job["input_path"], key, "recorded_original")
        reference(job["output_path"], key, "recorded_output")
        batch = job["batch_id"]
        if batch:
            if isinstance(batch, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", batch):
                bind(("outputs", "batch-" + batch + ".zip"), key, "batch_download_bundle", required=False)
            else:
                uncertain = True

    # Repair metadata is private, never returned. Keep entire order directories
    # including old layouts; an absent/malformed order.json never means unpaid.
    repair_owners = {}
    for label, relative in list(entries):
        if label != "repair" or "/" not in relative or relative.split("/", 1)[1] != "order.json":
            continue
        owner = relative.split("/")[0]
        if not re.fullmatch(r"[0-9a-f]{32}", owner):
            uncertain = True
            continue
        try:
            descriptor = _open(roots["repair"] / relative)
            with os.fdopen(descriptor, "rb") as source:
                before_read = _identity(os.fstat(source.fileno()))
                raw = source.read(limits.max_metadata_bytes + 1)
                after_read = _identity(os.fstat(source.fileno()))
            expected = entries[("repair", relative)]
            if (before_read != after_read or before_read != expected["identity"]
                    or hashlib.sha256(raw).hexdigest() != expected["sha256"]):
                raise AuditError("source_changed")
            if len(raw) > limits.max_metadata_bytes:
                raise ValueError()
            saved = _json(raw.decode("utf-8"), limits.max_metadata_bytes)
            state = saved.get("status")
            if state not in {"paid", "repaired", "pending", "running", "pending_payment", "failed", "cancelled"}:
                raise ValueError()
            why = "repair_order_retained"
            if state in {"paid", "pending", "running", "pending_payment"}:
                why = "repair_active_or_unsettled"; active = True
            if state in {"paid", "repaired"} or saved.get("payment_confirmed_at") or saved.get("payment_confirmation_pending"):
                why = "repair_paid_record_retained"
            repair_owners[_opaque(owner)] = {why}
            bind(("repair", owner), owner, why, directory=True)
            for field in ("filename", "artifact_file"):
                name = saved.get(field)
                if name:
                    if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
                        raise ValueError()
                    bind(("repair", owner + "/" + name), owner, "repair_" + field)
            if state == "repaired" and not saved.get("artifact_file") and saved.get("download_filename"):
                name = saved["download_filename"]
                if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
                    raise ValueError()
                bind(("repair", owner + "/" + name), owner, "repair_legacy_artifact")
        except AuditError:
            raise
        except (ValueError, UnicodeError, OSError):
            uncertain = True
            issues.append({"owner": _opaque(owner), "reason": "invalid_repair_metadata"})
            bind(("repair", owner), owner, "invalid_repair_metadata", directory=True)

    result = []
    owner_reasons = {_opaque(key): value for key, value in reasons.items()} | repair_owners
    for (label, relative), entry in sorted(entries.items()):
        owners, why = set(), set()
        if (label, relative) in references:
            linked, causes = references[(label, relative)]; owners.update(linked); why.update(causes)
        components = relative.split("/")
        for length in range(1, len(components) + 1):
            scoped = prefixes.get((label, "/".join(components[:length])))
            if scoped:
                linked, causes = scoped; owners.update(linked); why.update(causes)
        for owner in owners:
            why.update(owner_reasons.get(owner, set()))
        if _infrastructure(Path(relative).name):
            classification = "protected_infrastructure"; why.add("runtime_or_sensitive_file")
        elif owners:
            classification = "protected_reference"
        elif uncertain or active:
            classification = "unknown_protected"
            why.add("metadata_uncertain" if uncertain else "active_writer_may_own_unregistered_files")
        elif (label in {"repair", "reduce_work"} or relative.split("/")[0].startswith((".execution-", ".pdf-prepared-", "."))):
            classification = "unknown_protected"; why.add("unregistered_private_or_legacy_artifact")
        else:
            classification = "unreferenced_review"; why.add("no_known_reference_not_deletion_proof")
        if entry["identity"][3] > 1 and entry["kind"] == "file":
            classification = "unknown_protected"; why.add("hardlink_requires_review")
        result.append({**entry, "classification": classification,
                       "owners": sorted(owners), "reasons": sorted(why)})
    return result, missing, issues


def audit(database, *, uploads, outputs, repair, reduce_work, limits=None):
    limits = limits or Limits()
    if type(limits) is not Limits:
        raise AuditError("invalid_limits")
    limits.validate()
    database = _alias(database)
    roots = {key: _alias(value) for key, value in {
        "uploads": uploads, "outputs": outputs, "repair": repair, "reduce_work": reduce_work}.items()}
    if any(left == right or left.is_relative_to(right) or right.is_relative_to(left)
           for index, left in enumerate(roots.values()) for right in list(roots.values())[index + 1:]):
        raise AuditError("overlapping_roots")
    if any(database == root or database.is_relative_to(root) for root in roots.values()):
        raise AuditError("database_inside_artifact_root")
    _backup_only(database)
    descriptor = _open(database)
    try:
        before = _fingerprint_fd(descriptor, limits.max_file_bytes)
        rows = _read_database(database, descriptor, limits)
        entries, root_ids = _scan(roots, limits)
        files, missing, issues = _classify(rows, roots, entries, limits)
        after_entries, after_root_ids = _scan(roots, limits)
        _backup_only(database)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if (before != _fingerprint_fd(descriptor, limits.max_file_bytes)
                or before != _fingerprint(database, limits.max_file_bytes)
                or entries != after_entries or root_ids != after_root_ids):
            raise AuditError("source_changed")
    finally:
        os.close(descriptor)
    return {"schema_version": SCHEMA, "read_only": True, "deletion_authorized": False,
            "ttl_policy": None, "consistent_scan": True,
            "database_sha256": before[1], "database_identity": before[0],
            "root_identities": root_ids, "known_schema_projection": sorted(TABLES),
            "counts": dict(Counter(item["classification"] for item in files)),
            "opaque_bytes_hashed": sum(item["identity"][4] for item in files if item["sha256"]),
            "files": files, "missing_references": missing, "issues": issues,
            "scope": "offline_backup_reference_inventory_not_payment_or_download_validation"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for name in ("database", "uploads", "outputs", "repair", "reduce-work"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = audit(args.database, uploads=args.uploads, outputs=args.outputs,
                       repair=args.repair, reduce_work=args.reduce_work)
    except (AuditError, OSError, sqlite3.Error, UnicodeError, ValueError, RecursionError) as exc:
        reason = exc.reason if isinstance(exc, AuditError) else "unreadable_or_invalid_input"
        print(json.dumps({"schema_version": SCHEMA, "read_only": True,
                          "deletion_authorized": False, "consistent_scan": False, "error": reason}))
        return 2
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
