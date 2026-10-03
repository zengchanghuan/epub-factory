"""Bounded local text-PDF conversion and atomic, no-clobber EPUB publication.

The parser runs without application configuration, a database, credentials or
network access. It is not OCR and never authorizes a payment by itself. Parsing
warnings and the lack of a hard memory limit remain explicit review signals.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

# The isolated executable uses only this trusted checkout as its import root.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.domain import pdf_text_preflight as guard

SCHEMA = "pdf-text-epub-v1"
_PARSER_WARNINGS = frozenset({"empty_password_encryption", "pages_without_extractable_text",
                              "late_text_overlay_preserved"})
_MESSAGES = {
    **guard._MESSAGES,
    "invalid_input": "无法完整读取此 PDF，未生成可交付文件。",
    "unsupported_layout": "PDF 含有尚不能可靠重排的版式，未生成可交付文件。",
    "unsupported_content": "PDF 含有尚不能完整保留的内容，未生成可交付文件。",
    "unsupported_image": "PDF 图片格式或依赖尚不支持完整保留。",
    "image_limit": "PDF 图片超过本次转换的资源限制。",
    "invalid_image": "PDF 图片无法完整解码。",
    "decoder_unavailable": "本地图片解码依赖不可用。",
    "no_text": "PDF 没有可靠文字层；扫描 OCR 暂不支持。",
    "invalid_output": "输出必须位于真实目录中且不能覆盖现有文件。",
    "validation_unavailable": "EPUB 校验工具不可用，结果未发布。",
    "validation_failed": "EPUB 成品校验未通过，结果未发布。",
    "layout_unsupported": "PDF 含有尚不能可靠重排的版式，未生成可交付文件。",
    "outline_unresolved": "PDF 目录无法可靠映射到正文，未生成可交付文件。",
    "image_unsupported": "PDF 图片格式或依赖尚不支持完整保留。",
    "extraction_forbidden": "PDF 不允许提取内容，未生成可交付文件。",
    "text_missing": "PDF 没有可靠文字层；扫描 OCR 暂不支持。",
    "invalid_text": "PDF 文字层无法完整保留，未生成可交付文件。",
    "input_limit": "PDF 超过本次转换的资源限制。",
}


class PdfConversionError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason if type(reason) is str and reason in _MESSAGES else "worker_failed"
        super().__init__(_MESSAGES[self.reason])


@dataclass(frozen=True)
class PdfConversionLimits:
    max_file_bytes: int = 50 * 1024 * 1024
    max_pages: int = 500
    timeout_seconds: float = 180.0
    max_page_chars: int = 1_000_000
    max_total_chars: int = 10_000_000
    max_result_bytes: int = 64 * 1024 * 1024


def _limits(value):
    value = PdfConversionLimits() if value is None else value
    if not isinstance(value, PdfConversionLimits):
        raise PdfConversionError("invalid_limits")
    for key, maximum in asdict(PdfConversionLimits()).items():
        current = getattr(value, key)
        if key == "timeout_seconds":
            valid = type(current) in (int, float) and math.isfinite(current) and 0 < current <= 300
        else:
            valid = type(current) is int and 1 <= current <= maximum
        if not valid:
            raise PdfConversionError("invalid_limits")
    return value


def _validate(epub, jar, *, deadline, cancel_check):
    from app.domain.pdf_epub_validation import validate_pdf_epub
    return validate_pdf_epub(epub, jar, deadline=deadline, cancel_check=cancel_check)


def _sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _read_report(path, source_digest, limits):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError()
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError()
        def pairs(items):
            result = dict(items)
            if len(result) != len(items):
                raise ValueError()
            return result
        report = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(report) is not dict:
            raise ValueError()
        if set(report) == {"error"}:
            raise PdfConversionError(report["error"])
        if report.get("schema_version") != SCHEMA or report.get("source_sha256") != source_digest:
            raise ValueError()
        if type(report.get("memory_limited")) is not bool:
            raise ValueError()
        if not re.fullmatch(r"[0-9a-f]{64}", report.get("output_sha256", "")):
            raise ValueError()
        maxima = {"page_count": limits.max_pages, "normalized_characters": limits.max_total_chars,
                  "zero_width_spaces_preserved": limits.max_total_chars, "image_assets": 10_000,
                  "image_placements": 20_000, "toc_entries": 5000, "paragraph_count": 200_000}
        for key, maximum in maxima.items():
            if type(report.get(key)) is not int or not 0 <= report[key] <= maximum:
                raise ValueError()
        if not report["page_count"] or not report["normalized_characters"]:
            raise ValueError()
        pages = report.get("pages")
        if type(pages) is not list or len(pages) != report["page_count"]:
            raise ValueError()
        safe_pages = []
        for number, page in enumerate(pages, 1):
            if type(page) is not dict or type(page.get("number")) is not int or page["number"] != number:
                raise ValueError()
            for key in ("text_sha256", "normalized_sha256"):
                if type(page.get(key)) is not str or not re.fullmatch(r"[0-9a-f]{64}", page[key]):
                    raise ValueError()
            for key, maximum in (("char_count", limits.max_page_chars),
                                 ("normalized_char_count", limits.max_page_chars), ("image_placements", 20_000)):
                if type(page.get(key)) is not int or not 0 <= page[key] <= maximum:
                    raise ValueError()
            if page["normalized_char_count"] > page["char_count"]:
                raise ValueError()
            safe_pages.append({key: page[key] for key in (
                "number", "text_sha256", "normalized_sha256", "char_count",
                "normalized_char_count", "image_placements")})
        if (sum(p["char_count"] for p in pages) > limits.max_total_chars
                or sum(p["normalized_char_count"] for p in pages) != report["normalized_characters"]
                or sum(p["image_placements"] for p in pages) != report["image_placements"]):
            raise ValueError()
        warnings = report.get("warnings")
        if (type(warnings) is not list or len(warnings) > 100
                or any(type(w) is not str or w not in _PARSER_WARNINGS for w in warnings)
                or len(warnings) != len(set(warnings))):
            raise ValueError()
        # Never pass unknown manuscript-bearing parser fields into order logs.
        report["pages"] = safe_pages
        return {key: report[key] for key in ("schema_version", "source_sha256", "output_sha256",
                "memory_limited", "pages", "warnings", *maxima)}
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise PdfConversionError("invalid_result") from None


def _output_parent(path):
    """Hold a descriptor to the exact destination; reject symlink ancestry."""
    path = Path(path).absolute()
    if sys.platform == "darwin" and str(path).startswith(("/tmp/", "/var/")):
        path = Path("/private") / str(path).lstrip("/")
    if ".." in path.parts or path.suffix.lower() != ".epub":
        raise PdfConversionError("invalid_output")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        try:
            os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return descriptor, path.name
        raise PdfConversionError("invalid_output")
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _publish(source, output, expected_sha, check):
    """Keep ownership until the caller finishes all fallible cleanup."""
    descriptor, name = _output_parent(output)
    temporary = ".pdf-publish-" + uuid.uuid4().hex
    created = False
    published = False
    publication_identity = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        created = True
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as destination, source.open("rb") as origin:
            for block in iter(lambda: origin.read(1024 * 1024), b""):
                check()
                digest.update(block)
                destination.write(block)
            destination.flush()
            os.fsync(destination.fileno())
        if digest.hexdigest() != expected_sha:
            raise PdfConversionError("invalid_result")
        check()
        # link is atomic and refuses to overwrite even if the name raced us.
        staged = os.stat(temporary, dir_fd=descriptor, follow_symlinks=False)
        publication_identity = (staged.st_dev, staged.st_ino)
        os.link(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
        published = True
        os.unlink(temporary, dir_fd=descriptor)
        created = False
        os.fsync(descriptor)
        check()
        yield
        # Worker/temporary-directory cleanup also belongs to this transaction.
        # A late cancellation must not leave a successful-looking artifact.
        check()
    except BaseException:
        if published:
            # Roll back only our inode, never a file replaced by another writer.
            try:
                final = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if publication_identity == (final.st_dev, final.st_ino):
                    os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        raise
    finally:
        if created:
            os.unlink(temporary, dir_fd=descriptor)
        os.close(descriptor)


def convert_text_pdf(source, output, *, limits=None, cancel_check=None, epubcheck_jar=None):
    """Create one source-preserving EPUB, or leave no new deliverable on failure.

    Output must not already exist. The caller owns its destination directory.
    The receipt is an integrity record, not a semantic OCR/translation claim.
    """
    limits = _limits(limits)
    deadline = time.monotonic() + limits.timeout_seconds
    check = lambda: guard._check(deadline, cancel_check)
    process = None
    try:
        check()
        fd, _ = _output_parent(output)
        os.close(fd)
        # ExitStack is deliberately outside TemporaryDirectory: its publication
        # context rolls back even when worker or temporary cleanup raises.
        with ExitStack() as publication, tempfile.TemporaryDirectory(prefix="pdf_conversion_") as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            snapshot, epub, result = root / "source.pdf", root / "result.epub", root / "report.json"
            digest = guard._snapshot(source, snapshot, limits, deadline, cancel_check)
            env = {"PATH": os.defpath, "HOME": directory, "TMPDIR": directory,
                   "TMP": directory, "TEMP": directory, "LANG": "C", "LC_ALL": "C"}
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker",
                 str(snapshot), str(epub), str(result), json.dumps(asdict(limits))],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=directory, env=env, start_new_session=True)
            try:
                while process.poll() is None:
                    check()
                    if epub.exists() and epub.stat().st_size > limits.max_result_bytes:
                        raise PdfConversionError("result_limit")
                    time.sleep(0.025)
                check()
                if process.returncode != 0:
                    raise PdfConversionError("worker_failed")
                report = _read_report(result, digest, limits)
                if (epub.is_symlink() or not epub.is_file() or not 0 < epub.stat().st_size <= limits.max_result_bytes
                        or _sha(epub) != report["output_sha256"]):
                    raise PdfConversionError("invalid_result")
                check()
                jar = epubcheck_jar or os.environ.get("EPUBCHECK_JAR") or (
                    Path(__file__).parents[3] / "tools" / "epubcheck-5.1.0" / "epubcheck.jar")
                validation = _validate(epub, jar, deadline=deadline, cancel_check=cancel_check)
                check()
                if not validation.passed:
                    reason = "validation_unavailable" if validation.error_code == "EPUB_VALIDATION_UNAVAILABLE" else "validation_failed"
                    raise PdfConversionError(reason)
                report["epubcheck_warnings"] = validation.warnings
                report["validation_passed"] = True
                if validation.warnings:
                    report["warnings"] = sorted(set(report["warnings"]) | {"epubcheck_warnings"})
                if not report["memory_limited"]:
                    report["warnings"] = sorted(set(report["warnings"]) | {"memory_limit_unavailable"})
                report["requires_review"] = bool(report["warnings"])
                report["eligible_for_payment"] = not report["requires_review"]
                publication.enter_context(_publish(epub, output, report["output_sha256"], check))
                return report
            finally:
                guard._terminate_worker(process)
    except guard.PdfPreflightError as exc:
        raise PdfConversionError(exc.reason) from None
    except OSError:
        raise PdfConversionError("invalid_output") from None


def _worker(argv):
    source, output, result, raw_limits = argv
    limits = _limits(PdfConversionLimits(**json.loads(raw_limits)))
    os.umask(0o077)
    try:
        memory_limited = guard._child_sandbox(limits)
        from app.domain.pdf_reflow import build_text_pdf
        report = build_text_pdf(Path(source), Path(output), max_pages=limits.max_pages,
                                max_page_chars=limits.max_page_chars,
                                max_total_chars=limits.max_total_chars)
        report["memory_limited"] = memory_limited
    except Exception as exc:
        reason = getattr(exc, "reason", "worker_failed")
        report = {"error": reason if reason in _MESSAGES else "unsupported_content"}
    encoded = json.dumps(report, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > 2 * 1024 * 1024:
        encoded = b'{"error":"result_limit"}'
    with open(result, "xb") as stream:
        stream.write(encoded)


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    _worker(sys.argv[2:])
