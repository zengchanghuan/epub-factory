#!/usr/bin/env python3
"""Read-only deployment inventory; send through SSH stdin with the server venv.

Print only an allowlisted summary, never environment values, credentials, raw
database URLs, systemd commands, book names, customer data or exception messages.
No application imports, schema initialization, Redis writes or service changes.
SQLite mode=ro forbids database updates but can maintain SQLite's WAL shared
memory sidecar. Do not use immutable=1 for an active WAL database.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def property_value(service, name):
    return subprocess.check_output(
        ["systemctl", "show", service, "-p", name, "--value"],
        text=True, stderr=subprocess.DEVNULL, timeout=10,
    ).strip()


def effective_env(service, fallback):
    try:
        pid = int(property_value(service, "MainPID"))
        if pid <= 0:
            return dict(fallback), "dotenv-only-no-running-process"
        raw = Path(f"/proc/{pid}/environ").read_bytes()
        live = dict(part.decode().split("=", 1) for part in raw.split(b"\0") if b"=" in part)
        return {**fallback, **live}, "process-environment-with-dotenv-fallback"
    except Exception:
        return dict(fallback), "dotenv-only-process-unreadable"


def boolean(env, key):
    return str(env.get(key) or "").lower() in {"1", "true", "yes", "on"}


def positive(env, key):
    try:
        return int(env.get(key) or 0) > 0
    except (ValueError, TypeError):
        return False


def directory(path):
    symlink = Path(path).is_symlink()
    path = Path(path).resolve()
    return {
        "path": str(path), "exists": path.is_dir(),
        "temporary_location": str(path).startswith(("/tmp/", "/var/tmp/", "/run/")),
        "is_symlink": symlink,
    }


def main():
    root = Path(sys.argv[1]).resolve()
    backend = root / "backend"
    os.chdir(backend)
    configured = dict(dotenv_values(backend / ".env"))
    env, provenance = effective_env("epub-factory", configured)
    summary = {"config_source": provenance, "services": {},
               "runtime": {"python": ".".join(map(str, sys.version_info[:3])),
                           "sqlite": sqlite3.sqlite_version}}
    for service in ("epub-factory", "epub-factory-worker", "epub-factory-housekeeping", "epub-factory-beat"):
        summary["services"][service] = {
            name: property_value(service, name)
            for name in ("LoadState", "ActiveState", "SubState")
        }
    secret = str(env.get("JWT_SECRET") or "").strip()
    summary["jwt_configured_nonplaceholder"] = bool(secret) and secret not in {
        "CHANGE_ME_IN_PRODUCTION_PLEASE", "replace_with_a_long_random_secret_key",
    }
    summary["rate_limiter"] = {
        "enabled": boolean(env, "EPUB_LLM_RATE_LIMITER_ENABLED"),
        "fail_open": boolean(env, "EPUB_LLM_RATE_LIMIT_FAIL_OPEN"),
        "positive_rpm": positive(env, "EPUB_LLM_RPM"),
        "positive_tpm": positive(env, "EPUB_LLM_TPM"),
    }
    url = str(env.get("DATABASE_URL") or "sqlite:///./epub_jobs.db")
    dialect = url.partition(":")[0].split("+")[0]
    summary["database"] = {"dialect": dialect if dialect in {
        "sqlite", "postgresql", "postgres", "mysql", "mariadb", "mssql", "oracle",
    } else "unknown"}
    if url.startswith("sqlite:///"):
        db = Path(url[len("sqlite:///"):]).resolve()
        summary["database"].update(path=str(db), exists=db.is_file())
        try:
            with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
                check = conn.execute("PRAGMA quick_check").fetchone()[0]
                summary["database"]["integrity_check"] = "ok" if check == "ok" else "failed"
                counts = dict(conn.execute(
                    "SELECT status, COUNT(*) FROM epub_jobs GROUP BY status"
                ).fetchall())
                statuses = {"awaiting_confirm", "confirming", "pending_payment", "pending", "running", "success", "failed", "cancelled"}
                summary["database"]["job_status_counts"] = {key: value for key, value in counts.items() if key in statuses}
                summary["database"]["unknown_status_count"] = sum(value for key, value in counts.items() if key not in statuses)
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                summary["database"]["journal_mode"] = mode if mode in {"delete", "truncate", "persist", "memory", "wal", "off"} else "unknown"
        except Exception as exc:
            summary["database"]["read_error_type"] = type(exc).__name__
    else:
        summary["database"]["inventory_only"] = True
    summary["redis"] = {}
    for key in ("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        value = str(env.get(key) or "")
        try:
            parsed = urlsplit(value)
            loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            summary["redis"][key] = {"configured": bool(value), "scheme": "unknown",
                                      "loopback": False, "read_error_type": "ValueError"}
            continue
        item = {"configured": bool(value), "scheme": parsed.scheme if parsed.scheme in {"redis", "rediss", "unix"} else "unknown",
                "loopback": loopback}
        if parsed.scheme in {"redis", "rediss"}:
            try:
                import redis
                client = redis.Redis.from_url(value, socket_connect_timeout=3, socket_timeout=3)
                item["ping"] = bool(client.ping())
                version = client.info("server").get("redis_version")
                item["server_version"] = version if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) else "unknown"
                client.close()
            except Exception as exc:
                item["read_error_type"] = type(exc).__name__
        summary["redis"][key] = item
    summary["directories"] = {
        "uploads": directory(backend / "uploads"),
        "outputs": directory(backend / "outputs"),
        "repair": directory(env.get("REPAIR_UPLOAD_DIR") or "/tmp/epub-repair"),
        "deployment_backups": directory(root / "deploy-backups"),
    }
    summary["cache_files"] = {}
    for label, value in {
        "translation_cache": backend / "translation_cache.db",
        "translation_checkpoint": env.get("EPUB_TRANSLATION_CHECKPOINT_DB") or backend / "translation_cache.db",
    }.items():
        path = Path(value).resolve()
        summary["cache_files"][label] = {"path": str(path), "exists": path.is_file(),
            "temporary_location": str(path).startswith(("/tmp/", "/var/tmp/", "/run/"))}
    repair = Path(env.get("REPAIR_UPLOAD_DIR") or "/tmp/epub-repair")
    repair_counts = {}
    errors = legacy = 0
    if repair.is_dir():
        for child in repair.iterdir():
            if not child.is_dir() or child.is_symlink() or child.name.startswith("."):
                continue
            metadata = child / "order.json"
            if not metadata.is_file():
                legacy += 1
                continue
            try:
                status = json.loads(metadata.read_text(encoding="utf-8")).get("status")
                if status not in {"paid", "pending", "running", "pending_payment", "repaired", "failed"}:
                    errors += 1
                    continue
                repair_counts[status] = repair_counts.get(status, 0) + 1
            except Exception:
                errors += 1
    summary["repair_inventory"] = {"persisted_status_counts": repair_counts,
        "unreadable_metadata": errors, "directories_without_metadata": legacy,
        "legacy_memory_state_verified": False}
    worker_env, worker_provenance = effective_env("epub-factory-worker", configured)
    summary["worker_config_source"] = worker_provenance
    summary["api_worker_config_equal"] = {key: env.get(key) == worker_env.get(key)
        for key in ("DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL", "JWT_SECRET", "REPAIR_UPLOAD_DIR")}
    summary["backup_restore_verified"] = False
    summary["application_imported"] = False
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
