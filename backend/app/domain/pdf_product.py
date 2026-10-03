"""Private, frozen PDF conversion product. No gateway or model calls live here.

Preparation creates a private artifact, not an output_path. Confirmation binds
that exact artifact; delivery only copies verified bytes and never reparses it.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import uuid
import zipfile

from app.cancellation import JobCancelled, raise_if_cancelled
from app.models import ConversionResult
from . import pdf_conversion
from .pdf_text_preflight import _open_source, _source_identity, PdfPreflightError
from .payment_entitlement import VERIFIED_SOURCES, manual_payment_guard, same_amount

PRODUCT = "pdf_text_conversion"
ACKNOWLEDGEABLE_WARNINGS = frozenset({"empty_password_encryption", "pages_without_extractable_text",
                                    "late_text_overlay_preserved"})
_WARNINGS = ACKNOWLEDGEABLE_WARNINGS | {"memory_limit_unavailable", "epubcheck_warnings"}
_BASE = {"schema_version", "product", "phase", "source_sha256", "source_bytes", "amount"}
_PREPARED = {"artifact_id", "artifact_sha256", "artifact_bytes", "versions", "report", "plan_id"}
_CONFIRMED = {"accepted_warnings", "confirmed_at"}
_COUNTS = {"page_count": 500, "normalized_characters": 10_000_000,
           "zero_width_spaces_preserved": 10_000_000, "image_assets": 10_000,
           "image_placements": 20_000, "toc_entries": 5000, "paragraph_count": 200_000}
_VERSION_KEYS = {"parser", "package", "guard", "parser_sha256", "images_sha256", "validator_sha256",
                 "pypdf", "pdfplumber", "pillow", "epubcheck", "epubcheck_sha256"}
_MESSAGES = {
    "invalid_plan": "PDF 转换记录无效，请重新上传或联系管理员。",
    "unsupported_product": "此任务不是受支持的单本原文 PDF 转换。",
    "source_changed": "PDF 原稿缺失或已变化，请勿付款，请重新上传。",
    "artifact_changed": "预备 EPUB 缺失或已变化，已停止付款和交付。",
    "invalid_artifact_root": "PDF 私有存储不可用，请联系管理员。",
    "confirmation_required": "请先确认本次 PDF 转换结果和已提示的风险。",
    "review_blocked": "此 PDF 仍有不能通过确认解除的风险，当前不能收费。",
    "payment_required": "尚未核验本订单付款，不能交付 PDF 转换结果。",
    "invalid_output": "无法安全发布 PDF 转换结果。",
    "preparation_failed": "PDF 转换准备未完成，没有创建付款订单。",
}


class PdfProductError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in _MESSAGES else "invalid_plan"
        super().__init__(_MESSAGES[self.reason])


def _fail(reason):
    raise PdfProductError(reason)


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _amount(value):
    try:
        if type(value) is not str or not re.fullmatch(r"(?:0|[1-9][0-9]{0,7})\.[0-9]{2}", value):
            raise ValueError()
        if not Decimal(value).is_finite() or Decimal(value) <= 0:
            raise ValueError()
        return value
    except (ValueError, InvalidOperation):
        _fail("invalid_plan")


def new_pdf_plan(source_sha256, source_bytes, amount):
    return validate_pdf_plan({"schema_version": 1, "product": PRODUCT, "phase": "preparing",
                              "source_sha256": source_sha256, "source_bytes": source_bytes, "amount": amount})


def pdf_plan_identity(plan):
    immutable = {key: value for key, value in plan.items()
                 if key not in {"phase", "plan_id", "confirmed_at", "accepted_warnings"}}
    try:
        return hashlib.sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError, RecursionError):
        _fail("invalid_plan")


def _report(value):
    keys = {*_COUNTS, "warnings", "memory_limited", "validation_passed", "epubcheck_warnings",
            "requires_review", "eligible_for_payment"}
    if type(value) is not dict or set(value) != keys:
        _fail("invalid_plan")
    for key, maximum in _COUNTS.items():
        if type(value[key]) is not int or not 0 <= value[key] <= maximum:
            _fail("invalid_plan")
    if not value["page_count"] or not value["normalized_characters"]:
        _fail("invalid_plan")
    if any(type(value[key]) is not bool for key in ("memory_limited", "validation_passed", "requires_review", "eligible_for_payment")):
        _fail("invalid_plan")
    warnings = value["warnings"]
    if (type(warnings) is not list or any(type(w) is not str or w not in _WARNINGS for w in warnings)
            or len(set(warnings)) != len(warnings)
            or type(value["epubcheck_warnings"]) is not int or not 0 <= value["epubcheck_warnings"] <= 1_000_000
            or not value["validation_passed"]
            or (not value["memory_limited"]) != ("memory_limit_unavailable" in warnings)
            or bool(value["epubcheck_warnings"]) != ("epubcheck_warnings" in warnings)
            or value["requires_review"] != bool(warnings)
            or value["eligible_for_payment"] != (not bool(warnings))):
        _fail("invalid_plan")
    return deepcopy(value)


def validate_pdf_plan(plan, *, phase=None):
    """Pure schema/fingerprint validation, safe to use inside a store CAS."""
    if (type(plan) is not dict or type(plan.get("phase")) is not str
            or plan.get("phase") not in {"preparing", "prepared", "confirmed"}):
        _fail("invalid_plan")
    state = plan["phase"]
    fields = _BASE | (_PREPARED if state != "preparing" else set()) | (_CONFIRMED if state == "confirmed" else set())
    if (set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or plan["product"] != PRODUCT or (phase is not None and state != phase)
            or not _digest(plan["source_sha256"]) or type(plan["source_bytes"]) is not int
            or not 0 < plan["source_bytes"] <= 50 * 1024 * 1024):
        _fail("invalid_plan")
    _amount(plan["amount"])
    if state != "preparing":
        if (type(plan["artifact_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", plan["artifact_id"])
                or not _digest(plan["artifact_sha256"]) or type(plan["artifact_bytes"]) is not int
                or not 0 < plan["artifact_bytes"] <= 64 * 1024 * 1024
                or not _digest(plan["plan_id"]) or plan["plan_id"] != pdf_plan_identity(plan)):
            _fail("invalid_plan")
        versions = plan["versions"]
        if type(versions) is not dict or set(versions) != _VERSION_KEYS:
            _fail("invalid_plan")
        for key, value in versions.items():
            if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}", value):
                _fail("invalid_plan")
            if key.endswith("_sha256") and not _digest(value):
                _fail("invalid_plan")
        _report(plan["report"])
    if state == "confirmed":
        accepted = plan["accepted_warnings"]
        if (type(accepted) is not list or any(type(w) is not str for w in accepted)
                or len(set(accepted)) != len(accepted) or set(accepted) != set(plan["report"]["warnings"])
                or set(accepted) - ACKNOWLEDGEABLE_WARNINGS):
            _fail("invalid_plan")
        try:
            at = datetime.fromisoformat(plan["confirmed_at"])
            if at.tzinfo is None or at.utcoffset().total_seconds() != 0:
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            _fail("invalid_plan")
        _billable(plan)
    return deepcopy(plan)


def is_pdf_job(job):
    stats = getattr(job, "translation_stats", None)
    return ((isinstance(stats, dict) and "pdf_conversion" in stats)
            or getattr(getattr(job, "output_mode", None), "value", getattr(job, "output_mode", None)) == "original"
            or Path(str(getattr(job, "input_path", ""))).suffix.lower() == ".pdf"
            or Path(str(getattr(job, "source_filename", ""))).suffix.lower() == ".pdf")


def _job_plan(job):
    if (getattr(getattr(job, "output_mode", None), "value", getattr(job, "output_mode", None)) != "original"
            or getattr(job, "enable_translation", False) or getattr(job, "enable_precision_polish", False)
            or getattr(job, "bilingual", False) or getattr(job, "batch_id", "")
            or Path(str(getattr(job, "input_path", ""))).suffix.lower() != ".pdf"):
        _fail("unsupported_product")
    stats = getattr(job, "translation_stats", None)
    plan = validate_pdf_plan(stats.get("pdf_conversion") if type(stats) is dict else None)
    if plan["amount"] != getattr(job, "expected_amount", None):
        _fail("invalid_plan")
    return plan


def validate_pdf_job_plan(job):
    """Pure product/plan validation for atomic store transitions (no file IO)."""
    return _job_plan(job)


def _fingerprint(path, *, maximum, reason, cancel_check=None):
    try:
        with os.fdopen(_open_source(path), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not 0 < before.st_size <= maximum:
                _fail(reason)
            digest, count = hashlib.sha256(), 0
            for data in iter(lambda: stream.read(1024 * 1024), b""):
                raise_if_cancelled(cancel_check)
                count += len(data)
                if count > maximum:
                    _fail(reason)
                digest.update(data)
            if count != before.st_size or _source_identity(before) != _source_identity(os.fstat(stream.fileno())):
                _fail(reason)
            raise_if_cancelled(cancel_check)
            return digest.hexdigest(), count
    except (PdfPreflightError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PdfProductError):
            raise
        _fail(reason)


def _artifact_path(plan, artifact_root):
    return Path(artifact_root) / (".pdf-prepared-" + plan["artifact_id"]) / "book.epub"


def _billable(plan):
    if plan["phase"] == "preparing":
        _fail("confirmation_required")
    report = plan["report"]
    if (not report["memory_limited"] or not report["validation_passed"] or report["epubcheck_warnings"]
            or set(report["warnings"]) - ACKNOWLEDGEABLE_WARNINGS):
        _fail("review_blocked")


def validate_pdf_job(job, artifact_root, *, require_confirmed=False, require_billable=False):
    plan = _job_plan(job)
    if require_confirmed and plan["phase"] != "confirmed":
        _fail("confirmation_required")
    if _fingerprint(job.input_path, maximum=50 * 1024 * 1024, reason="source_changed") != (plan["source_sha256"], plan["source_bytes"]):
        _fail("source_changed")
    if plan["phase"] != "preparing":
        if _fingerprint(_artifact_path(plan, artifact_root), maximum=64 * 1024 * 1024,
                        reason="artifact_changed") != (plan["artifact_sha256"], plan["artifact_bytes"]):
            _fail("artifact_changed")
    if require_billable:
        _billable(plan)
    return plan


def _versions(jar):
    here = Path(__file__).parent
    try:
        with os.fdopen(_open_source(jar), "rb") as stream, zipfile.ZipFile(stream) as archive:
            info = archive.getinfo("META-INF/MANIFEST.MF")
            if info.file_size > 32 * 1024:
                raise ValueError()
            manifest = archive.read(info).decode("utf-8")
        matches = re.findall(r"^Implementation-Version: ([0-9]+\.[0-9]+\.[0-9]+)\r?$", manifest, re.M)
        if len(matches) != 1:
            raise ValueError()
    except (OSError, PdfPreflightError, ValueError, KeyError, zipfile.BadZipFile):
        _fail("preparation_failed")
    return {"parser": "pdf-reflow-v1", "package": pdf_conversion.SCHEMA, "guard": "pdf-conversion-v1",
            "parser_sha256": hashlib.sha256((here / "pdf_reflow.py").read_bytes()).hexdigest(),
            "images_sha256": hashlib.sha256((here / "pdf_image_assets.py").read_bytes()).hexdigest(),
            "validator_sha256": hashlib.sha256((here / "pdf_epub_validation.py").read_bytes()).hexdigest(),
            "pypdf": version("pypdf"), "pdfplumber": version("pdfplumber"), "pillow": version("Pillow"),
            "epubcheck": matches[0], "epubcheck_sha256": _fingerprint(jar, maximum=50 * 1024 * 1024,
                                                                      reason="preparation_failed")[0]}


def prepare_pdf_artifact(job, artifact_root, cancel_check=None):
    plan = validate_pdf_job(job, artifact_root)
    if plan["phase"] != "preparing":
        _fail("invalid_plan")
    raise_if_cancelled(cancel_check)
    identity = uuid.uuid4().hex
    directory = Path(artifact_root) / (".pdf-prepared-" + identity)
    parent_fd = None
    try:
        # Reuse B2's component-by-component directory checks; the probe name is
        # never created. The only newly created directory belongs to this call.
        parent_fd, _ = pdf_conversion._output_parent(Path(artifact_root) / (".probe-" + identity + ".epub"))
        os.mkdir(directory.name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except (OSError, pdf_conversion.PdfConversionError):
        _fail("invalid_artifact_root")
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    # Failures after this point may leave a private orphan, never a public
    # deliverable. Do not risk deleting another writer's inode during cleanup.
    jar = Path(os.environ.get("EPUBCHECK_JAR") or Path(__file__).parents[3] / "tools/epubcheck-5.1.0/epubcheck.jar")
    versions = _versions(jar)
    try:
        report = pdf_conversion.convert_text_pdf(Path(job.input_path), directory / "book.epub",
                                                 cancel_check=cancel_check, epubcheck_jar=jar)
    except pdf_conversion.PdfConversionError as exc:
        if exc.reason == "cancelled":
            raise JobCancelled("PDF 准备已取消") from None
        raise
    raise_if_cancelled(cancel_check)
    if versions != _versions(jar):
        _fail("preparation_failed")
    if report.get("source_sha256") != plan["source_sha256"]:
        _fail("source_changed")
    fingerprint = _fingerprint(directory / "book.epub", maximum=64 * 1024 * 1024,
                               reason="artifact_changed", cancel_check=cancel_check)
    if fingerprint[0] != report.get("output_sha256"):
        _fail("artifact_changed")
    keys = {*_COUNTS, "warnings", "memory_limited", "validation_passed", "epubcheck_warnings",
            "requires_review", "eligible_for_payment"}
    try:
        summary = _report({key: report[key] for key in keys})
    except (KeyError, TypeError):
        _fail("preparation_failed")
    prepared = {**plan, "phase": "prepared", "artifact_id": identity,
                "artifact_sha256": fingerprint[0], "artifact_bytes": fingerprint[1],
                "versions": versions, "report": summary}
    prepared["plan_id"] = pdf_plan_identity(prepared)
    # The original may have been replaced while the isolated worker used its
    # snapshot. Never offer confirmation for a different retained upload.
    if _fingerprint(job.input_path, maximum=50 * 1024 * 1024, reason="source_changed",
                    cancel_check=cancel_check) != (plan["source_sha256"], plan["source_bytes"]):
        _fail("source_changed")
    validated = validate_pdf_plan(prepared, phase="prepared")
    raise_if_cancelled(cancel_check)
    return validated


def confirm_pdf_plan(job, artifact_root, plan_id, accepted_warnings):
    plan = validate_pdf_job(job, artifact_root, require_billable=True)
    if plan["phase"] != "prepared" or not _digest(plan_id) or plan_id != plan["plan_id"]:
        _fail("confirmation_required")
    if (type(accepted_warnings) is not list or any(type(w) is not str for w in accepted_warnings)
            or len(set(accepted_warnings)) != len(accepted_warnings)
            or set(accepted_warnings) != set(plan["report"]["warnings"])):
        _fail("confirmation_required")
    return validate_pdf_plan({**plan, "phase": "confirmed", "accepted_warnings": sorted(accepted_warnings),
                              "confirmed_at": datetime.now(timezone.utc).isoformat()}, phase="confirmed")


def _require_paid(job, plan):
    if (type(getattr(job, "payment_resolution", None)) is not dict
            or type(getattr(job, "payment_entitlement", None)) is not dict):
        _fail("payment_required")
    if manual_payment_guard(job):
        _fail("payment_required")
    resolution = getattr(job, "payment_resolution", None) or {}
    if (resolution.get("state") == "paid" and resolution.get("source") in VERIFIED_SOURCES
            and same_amount(resolution.get("amount"), plan["amount"])):
        return
    entitlement = getattr(job, "payment_entitlement", None) or {}
    if (getattr(job, "is_test_order", False) is True and entitlement.get("state") == "test_authorized"
            and entitlement.get("source") == "server_test_bypass" and entitlement.get("order_no") == job.id
            and same_amount(entitlement.get("amount"), plan["amount"])):
        return
    _fail("payment_required")


def validate_pdf_delivery(job, output_path):
    plan = _job_plan(job)
    if plan["phase"] != "confirmed":
        _fail("confirmation_required")
    _require_paid(job, plan)
    if _fingerprint(output_path, maximum=64 * 1024 * 1024, reason="artifact_changed") != (plan["artifact_sha256"], plan["artifact_bytes"]):
        _fail("artifact_changed")


def public_pdf_summary(job):
    if not is_pdf_job(job):
        return None
    try:
        plan = _job_plan(job)
    except PdfProductError:
        return {"phase": "invalid", "can_confirm": False, "blocked_reason": "invalid_plan",
                "preserves_original": True, "confirmed": False, "warnings": []}
    report = plan.get("report", {})
    blocked = ""
    try:
        _billable(plan)
    except PdfProductError as exc:
        blocked = exc.reason
    return {"phase": plan["phase"], "plan_id": plan.get("plan_id"), "amount": plan["amount"],
            **{key: report.get(key, 0) for key in ("page_count", "normalized_characters", "image_assets", "toc_entries")},
            "warnings": list(report.get("warnings", [])), "can_confirm": (
                plan["phase"] == "prepared" and not blocked
                and getattr(getattr(job, "status", None), "value", getattr(job, "status", None)) == "awaiting_confirm"),
            "blocked_reason": blocked, "preserves_original": True, "confirmed": plan["phase"] == "confirmed"}


def copy_prepared_pdf(job, output_path, artifact_root, cancel_check=None):
    plan = validate_pdf_job(job, artifact_root, require_confirmed=True, require_billable=True)
    _require_paid(job, plan)
    raise_if_cancelled(cancel_check)
    descriptor = None
    temporary = ".pdf-copy-" + uuid.uuid4().hex
    staged = published = False
    identity = None
    try:
        descriptor, name = pdf_conversion._output_parent(output_path)
        with os.fdopen(_open_source(_artifact_path(plan, artifact_root)), "rb") as source:
            before = os.fstat(source.fileno())
            if before.st_size != plan["artifact_bytes"]:
                _fail("artifact_changed")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            staged = True
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    raise_if_cancelled(cancel_check)
                    size += len(chunk)
                    if size > plan["artifact_bytes"]:
                        _fail("artifact_changed")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if (size != plan["artifact_bytes"] or digest.hexdigest() != plan["artifact_sha256"]
                    or _source_identity(before) != _source_identity(os.fstat(source.fileno()))):
                _fail("artifact_changed")
        raise_if_cancelled(cancel_check)
        info = os.stat(temporary, dir_fd=descriptor, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        os.link(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
        published = True
        os.unlink(temporary, dir_fd=descriptor)
        staged = False
        os.fsync(descriptor)
        raise_if_cancelled(cancel_check)
        return ConversionResult(translation_stats=deepcopy(job.translation_stats), validation_passed=True,
                                message="PDF 已转换为 EPUB，保留原文，未调用翻译或 OCR。")
    except BaseException as exc:
        if published:
            try:
                actual = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (actual.st_dev, actual.st_ino) == identity:
                    os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        if isinstance(exc, (OSError, PdfPreflightError, pdf_conversion.PdfConversionError)):
            _fail("invalid_output")
        raise
    finally:
        if descriptor is not None:
            try:
                if staged:
                    os.unlink(temporary, dir_fd=descriptor)
            finally:
                os.close(descriptor)
