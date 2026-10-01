"""Repair execution and artifact publication behind a held executor lease.

The existing repair engine is unchanged. Only an owner-matching, still-paid
order can publish its unique artifact; a leftover file is not delivery proof.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import re
import time
import zipfile

logger = logging.getLogger("epub_factory")


def source_path(root: Path, job_id: str, job: dict):
    directory = root / job_id
    if directory.is_symlink():
        return None
    filename = job.get("filename")
    if filename and Path(filename).name == filename:
        candidate = directory / filename
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    if filename:
        # An explicit source is authoritative. A stray EPUB is not a substitute
        # when the recorded original has gone missing.
        return None
    sources = [p for p in directory.glob("*.epub")
               if "_fixed" not in p.stem and not p.name.startswith(".")
               and not p.is_symlink() and p.is_file()]
    return sources[0] if len(sources) == 1 else None


def artifact_path(root: Path, job_id: str, job: dict):
    """Resolve one committed artifact, including old explicitly delivered files."""
    if job.get("status") != "repaired":
        return None
    filename = job.get("artifact_file") or job.get("download_filename")
    if not filename or Path(filename).name != filename:
        return None
    directory = root / job_id
    path = directory / filename
    return path if not directory.is_symlink() and path.is_file() and not path.is_symlink() else None


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_repair(repository, job_id, owner, *, on_completed=None, max_attempts=3):
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise ValueError("Invalid repair execution owner")
    terminal = None
    with repository.transaction(job_id) as job:
        if not job or job.get("status") != "paid":
            return
        attempts = int(job.get("execution_attempts") or 0)
        if attempts >= max_attempts:
            job.update(status="failed", error="修复多次中断，请联系客服处理；付款记录已保留",
                       execution_state="attention_required")
            terminal = "failed"
        else:
            job.update(execution_owner=owner, execution_attempts=attempts + 1,
                       execution_started_at=time.time(), execution_state="running")
            snapshot = dict(job)
    if terminal:
        if on_completed:
            on_completed(job_id, terminal)
        return
    directory = repository.root / job_id
    temporary = directory / (".repair-" + owner + ".pending.epub")
    published = directory / (".repair-" + owner + ".epub")
    try:
        source = source_path(repository.root, job_id, snapshot)
        if source is None:
            raise ValueError("Repair source unavailable")
        from .epub_input_integrity import validate_epub_resources
        from ..engine.epub_repairer import repair
        with source.open("rb") as stream:
            validate_epub_resources(stream)
        repair(str(source), str(temporary))
        # Packaging integrity, not a claim that the narrow repair engine fixes
        # every EPUBCheck issue or every source defect.
        with zipfile.ZipFile(temporary) as archive:
            if (archive.testzip() is not None or archive.namelist()[0] != "mimetype"
                    or archive.getinfo("mimetype").compress_type != zipfile.ZIP_STORED
                    or archive.read("mimetype") != b"application/epub+zip"):
                raise ValueError("Repair artifact is incomplete")
        digest = file_sha256(temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        with repository.transaction(job_id) as current:
            if (not current or current.get("status") != "paid"
                    or current.get("execution_owner") != owner):
                return
            temporary.replace(published)
            current.update(status="repaired", artifact_file=published.name,
                           artifact_sha256=digest, download_filename=source.stem + "_fixed.epub",
                           execution_state="finished", execution_finished_at=time.time(), error=None)
        terminal = "repaired"
    except Exception as exc:
        logger.warning("Repair execution failed (%s)", type(exc).__name__, extra={"job_id": job_id})
        # If publication outcome is unknown, keep the private artifact. Never
        # delete a possibly committed file to compensate a metadata IO error.
        with repository.transaction(job_id) as current:
            if (current and current.get("status") == "paid"
                    and current.get("execution_owner") == owner):
                current.update(status="failed", error="修复失败，请联系客服；付款记录已保留",
                               execution_state="failed", execution_finished_at=time.time())
                terminal = "failed"
    finally:
        temporary.unlink(missing_ok=True)
    if terminal and on_completed:
        on_completed(job_id, terminal)
