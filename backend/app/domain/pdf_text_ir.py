"""Private, bounded PDF source IR; NOT paragraph reconstruction or a deliverable.

Text/metadata are untrusted manuscript content. Consumers must not execute them.
Image bytes are decrypted, pre-filter encoded streams, not source ciphertext or
standalone renderable pictures (color spaces, masks and placement are incomplete).
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

_SCHEMA = "pdf-text-ir-v1"
_PARSER = "6.0.0"
_BOOK_FLAGS = ("reading_order_unverified", "encoded_streams_only", "parser_warnings",
               "empty_password_encryption", "memory_limit_unavailable", "outline_unresolved")
_PAGE_FLAGS = ("no_extractable_text", "sparse_text", "replacement_characters", "control_characters",
               "private_use_characters", "parser_warnings", "nested_forms", "complex_geometry",
               "fragment_text_mismatch")
_RESOURCE_FLAGS = ("encoded_stream_only", "opaque_filter", "mask_dependency_present",
                   "color_space_dependency_present", "decode_parameters_present")
_EXTRA_ERRORS = {"fragment_limit": "PDF 文字片段超过本次记录限额。",
                 "resource_limit": "PDF 图像资源超过本次记录限额。",
                 "structure_limit": "PDF 结构超过本次记录限额。",
                 "unsupported_content": "PDF 包含当前不能安全记录的内容，未生成中间记录。"}


def _preflight():
    # Load a trusted adjacent file, not app/__init__, configuration, DB or services.
    name = "_isolated_pdf_text_preflight"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("pdf_text_preflight.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class PdfIrError(RuntimeError):
    def __init__(self, reason):
        messages = {**_preflight()._MESSAGES, **_EXTRA_ERRORS}
        self.reason = reason if type(reason) is str and reason in messages else "worker_failed"
        super().__init__(messages[self.reason])


@dataclass(frozen=True)
class PdfIrLimits:
    max_file_bytes: int = 50 * 1024 * 1024
    max_pages: int = 500
    timeout_seconds: float = 60.0
    max_page_chars: int = 1_000_000
    max_total_chars: int = 10_000_000
    max_fragments_per_page: int = 20_000
    max_total_fragments: int = 200_000
    max_resources: int = 10_000
    max_resource_bytes: int = 20 * 1024 * 1024
    max_total_resource_bytes: int = 32 * 1024 * 1024
    max_result_bytes: int = 64 * 1024 * 1024


def _validate_limits(limits):
    if not isinstance(limits, PdfIrLimits):
        raise PdfIrError("invalid_limits")
    maxima = asdict(PdfIrLimits())
    maxima.update(max_file_bytes=200 * 1024 * 1024, max_pages=2000,
                  max_page_chars=2_000_000, max_total_chars=20_000_000)
    for key, maximum in maxima.items():
        value = getattr(limits, key)
        if key == "timeout_seconds":
            valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= 300
        else:
            valid = type(value) is int and 1 <= value <= maximum
        if not valid:
            raise PdfIrError("invalid_limits")
    if limits.max_result_bytes < 128:
        raise PdfIrError("invalid_limits")
    return limits


def _page_id(digest, number):
    return f"{_SCHEMA}-{digest}-p{number:04d}"


def _resource_id(digest, number):
    return f"{_SCHEMA}-{digest}-image{number:06d}"


def _ordered(flags, choices):
    return [flag for flag in choices if flag in flags]


def _launch_worker(snapshot, result_path, limits):
    env = {"PATH": os.defpath, "HOME": str(snapshot.parent), "TMPDIR": str(snapshot.parent),
           "TMP": str(snapshot.parent), "TEMP": str(snapshot.parent), "LANG": "C", "LC_ALL": "C"}
    return subprocess.Popen(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker", str(snapshot),
         str(result_path), json.dumps(asdict(limits), separators=(",", ":"))],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=snapshot.parent, env=env, start_new_session=True)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1_000_000_000


def _text_flags(text):
    import unicodedata
    flags = set()
    nonspace = sum(not char.isspace() for char in text)
    if not nonspace:
        flags.add("no_extractable_text")
    elif nonspace < 40:
        flags.add("sparse_text")
    if "\ufffd" in text:
        flags.add("replacement_characters")
    if any(not char.isspace() and unicodedata.category(char) in {"Cc", "Cf"} for char in text):
        flags.add("control_characters")
    if any(unicodedata.category(char) == "Co" for char in text):
        flags.add("private_use_characters")
    return flags


def _validate_result(value, digest, limits, check=None):
    check = check or (lambda: None)
    check()
    def need(condition):
        if not condition:
            raise PdfIrError("invalid_result")
    def flags(items, allowed, required=()):
        need(type(items) is list and all(type(item) is str and item in allowed for item in items))
        need(items == _ordered(items, allowed) and all(item in items for item in required))
    def count(value, maximum):
        need(type(value) is int and 0 <= value <= maximum)
    need(type(value) is dict)
    if set(value) == {"error"}:
        need(type(value["error"]) is str and value["error"] in {**_preflight()._MESSAGES, **_EXTRA_ERRORS})
        raise PdfIrError(value["error"])
    keys = {"schema_version", "source_sha256", "parser_version", "status", "eligible_for_payment",
            "page_count", "total_char_count", "total_nonspace_count", "total_fragment_count",
            "total_encoded_resource_bytes", "flags", "metadata", "outline", "pages", "resources"}
    need(set(value) == keys and value["schema_version"] == _SCHEMA and value["source_sha256"] == digest
         and value["parser_version"] == _PARSER and value["status"] == "review_required"
         and value["eligible_for_payment"] is False)
    flags(value["flags"], _BOOK_FLAGS, _BOOK_FLAGS[:2])
    need("memory_limit_unavailable" not in value["flags"] or sys.platform == "darwin")
    count(value["page_count"], limits.max_pages)
    need(value["page_count"] > 0 and type(value["pages"]) is list and len(value["pages"]) == value["page_count"])
    need(type(value["metadata"]) is dict and set(value["metadata"]) == {"title", "author"})
    need(all(item is None or type(item) is str and len(item) <= 2048 for item in value["metadata"].values()))
    need(type(value["outline"]) is list and len(value["outline"]) <= 5000)
    for item in value["outline"]:
        check()
        need(type(item) is dict and set(item) == {"title", "page_number", "depth"})
        need(type(item["title"]) is str and len(item["title"]) <= 2048)
        count(item["depth"], 20)
        need(item["page_number"] is None or type(item["page_number"]) is int and 1 <= item["page_number"] <= value["page_count"])
        if item["page_number"] is None:
            need("outline_unresolved" in value["flags"])
    need(type(value["resources"]) is list and len(value["resources"]) <= limits.max_resources)
    resource_ids, object_ids, resource_bytes = set(), set(), 0
    resource_keys = {"resource_id", "object_id", "width", "height", "bits_per_component", "filters",
                     "encoded_stream_base64", "encoded_stream_sha256", "encoded_size", "flags"}
    for index, resource in enumerate(value["resources"], 1):
        check()
        need(type(resource) is dict and set(resource) == resource_keys)
        need(resource["resource_id"] == _resource_id(digest, index))
        resource_ids.add(resource["resource_id"])
        obj = resource["object_id"]
        if obj is not None:
            need(type(obj) is list and len(obj) == 2 and all(type(x) is int and 0 <= x <= 2**31 - 1 for x in obj))
            need(tuple(obj) not in object_ids)
            object_ids.add(tuple(obj))
        for key in ("width", "height", "bits_per_component"):
            need(resource[key] is None or type(resource[key]) is int and 0 < resource[key] <= 1_000_000_000)
        need(type(resource["filters"]) is list and len(resource["filters"]) <= 10
             and all(type(item) is str and item.startswith("/") and len(item) <= 128 for item in resource["filters"]))
        flags(resource["flags"], _RESOURCE_FLAGS, ("encoded_stream_only",))
        need(("opaque_filter" in resource["flags"]) == (resource["filters"] not in (["/DCTDecode"], ["/JPXDecode"])))
        count(resource["encoded_size"], limits.max_resource_bytes)
        need(type(resource["encoded_stream_base64"]) is str and type(resource["encoded_stream_sha256"]) is str)
        need(len(resource["encoded_stream_base64"]) == 4 * ((resource["encoded_size"] + 2) // 3))
        data = base64.b64decode(resource["encoded_stream_base64"], validate=True)
        need(len(data) == resource["encoded_size"] and base64.b64encode(data).decode("ascii") == resource["encoded_stream_base64"]
             and hashlib.sha256(data).hexdigest() == resource["encoded_stream_sha256"])
        resource_bytes += len(data)
        need(resource_bytes <= limits.max_total_resource_bytes)
    totals = {"char_count": 0, "nonspace_count": 0, "fragment_count": 0}
    referenced, fragment_chars = set(), 0
    page_keys = {"page_number", "page_id", "raw_text", "char_count", "nonspace_count", "fragments",
                 "image_resource_ids", "has_content_stream", "flags"}
    fragment_keys = {"fragment_id", "sequence", "text", "cm", "tm", "font_size", "font_name"}
    for number, page in enumerate(value["pages"], 1):
        check()
        need(type(page) is dict and set(page) == page_keys)
        need(type(page["page_number"]) is int and page["page_number"] == number and page["page_id"] == _page_id(digest, number))
        need(type(page["raw_text"]) is str and len(page["raw_text"]) <= limits.max_page_chars)
        count(page["char_count"], limits.max_page_chars); count(page["nonspace_count"], limits.max_page_chars)
        need(page["char_count"] == len(page["raw_text"]) and page["nonspace_count"] == sum(not x.isspace() for x in page["raw_text"]))
        need(type(page["has_content_stream"]) is bool)
        flags(page["flags"], _PAGE_FLAGS)
        need(set(page["flags"]) & set(_PAGE_FLAGS[:5]) == _text_flags(page["raw_text"]))
        need(type(page["image_resource_ids"]) is list and all(type(x) is str and x in resource_ids for x in page["image_resource_ids"]))
        need(len(set(page["image_resource_ids"])) == len(page["image_resource_ids"]))
        referenced.update(page["image_resource_ids"])
        need(type(page["fragments"]) is list and len(page["fragments"]) <= limits.max_fragments_per_page)
        text_parts, page_fragment_chars = [], 0
        for sequence, fragment in enumerate(page["fragments"], 1):
            if sequence % 128 == 0:
                check()
            need(type(fragment) is dict and set(fragment) == fragment_keys)
            need(type(fragment["sequence"]) is int and fragment["sequence"] == sequence
                 and fragment["fragment_id"] == page["page_id"] + f":f{sequence:06d}")
            need(type(fragment["text"]) is str and 0 < len(fragment["text"]) <= limits.max_page_chars)
            text_parts.append(fragment["text"]); page_fragment_chars += len(fragment["text"])
            for key in ("cm", "tm"):
                need(fragment[key] is None or type(fragment[key]) is list and len(fragment[key]) == 6 and all(_finite(x) for x in fragment[key]))
            need(fragment["font_size"] is None or _finite(fragment["font_size"]))
            need(fragment["font_name"] is None or type(fragment["font_name"]) is str and len(fragment["font_name"]) <= 1024)
        need(("fragment_text_mismatch" in page["flags"]) == ("".join(text_parts) != page["raw_text"]))
        need(page_fragment_chars <= limits.max_page_chars)
        fragment_chars += page_fragment_chars
        totals["char_count"] += page["char_count"]; totals["nonspace_count"] += page["nonspace_count"]
        totals["fragment_count"] += len(page["fragments"])
    need(referenced == resource_ids and fragment_chars <= limits.max_total_chars)
    for name, total in totals.items():
        maximum = limits.max_total_fragments if name == "fragment_count" else limits.max_total_chars
        count(value["total_" + name], maximum); need(value["total_" + name] == total)
    count(value["total_encoded_resource_bytes"], limits.max_total_resource_bytes)
    need(value["total_encoded_resource_bytes"] == resource_bytes)
    check()
    return value


def _read_result(path, digest, limits, check=None):
    check = check or (lambda: None)
    try:
        check()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PdfIrError("invalid_result")
            if info.st_size > limits.max_result_bytes:
                raise PdfIrError("result_limit")
            raw = stream.read(limits.max_result_bytes + 1)
        if len(raw) > limits.max_result_bytes:
            raise PdfIrError("result_limit")
        check()
        def pairs(items):
            result = dict(items)
            if len(result) != len(items):
                raise ValueError()
            return result
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        check()
        result = _validate_result(value, digest, limits, check)
        check()
        return result
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise PdfIrError("invalid_result") from None


def extract_pdf_text_ir(path: Path, *, limits: PdfIrLimits | None = None, cancel_check=None) -> dict:
    limits = _validate_limits(PdfIrLimits() if limits is None else limits)
    guard, process = _preflight(), None
    deadline = time.monotonic() + limits.timeout_seconds
    try:
        guard._check(deadline, cancel_check)
        with tempfile.TemporaryDirectory(prefix="pdf_text_ir_") as directory:
            root = Path(directory); os.chmod(root, 0o700)
            snapshot, result = root / "source.pdf", root / "ir.json"
            digest = guard._snapshot(path, snapshot, limits, deadline, cancel_check)
            guard._check(deadline, cancel_check)
            try:
                process = _launch_worker(snapshot, result, limits)
                while process.poll() is None:
                    guard._check(deadline, cancel_check)
                    try:
                        if result.lstat().st_size > limits.max_result_bytes:
                            raise PdfIrError("result_limit")
                    except FileNotFoundError:
                        pass
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
                guard._check(deadline, cancel_check)
                if process.returncode != 0:
                    raise PdfIrError("worker_failed")
                value = _read_result(result, digest, limits,
                                     lambda: guard._check(deadline, cancel_check))
                guard._check(deadline, cancel_check)
                return value
            finally:
                if process is not None:
                    guard._terminate_worker(process)
    except guard.PdfPreflightError as exc:
        raise PdfIrError(exc.reason) from None
    except OSError:
        raise PdfIrError("worker_failed") from None


def _parse_snapshot(source, limits, memory_limited):
    import importlib.metadata
    import logging
    import warnings
    try:
        if importlib.metadata.version("pypdf") != _PARSER:
            raise PdfIrError("parser_unavailable")
        from pypdf import PdfReader
        from pypdf.generic import ContentStream, NullObject
    except ImportError:
        raise PdfIrError("parser_unavailable") from None
    class Counter(logging.Handler):
        count = 0
        def emit(self, record):
            self.count += 1
    counter = Counter(level=logging.WARNING)
    logger = logging.getLogger("pypdf")
    logger.handlers, logger.propagate, logger.level = [counter], False, logging.WARNING
    warnings.showwarning = lambda *args, **kwargs: setattr(counter, "count", counter.count + 1)
    warnings.simplefilter("always")
    reader = PdfReader(str(source), strict=True)
    book_flags = set(_BOOK_FLAGS[:2])
    if not memory_limited:
        book_flags.add("memory_limit_unavailable")
    if reader.is_encrypted:
        _preflight()._allow_empty_password_extraction(reader)
        book_flags.add("empty_password_encryption")
    page_count = len(reader.pages)
    if not page_count:
        raise PdfIrError("empty_pdf")
    if page_count > limits.max_pages:
        raise PdfIrError("page_limit")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    resources, resource_index, pages = [], {}, []
    resource_bytes = fragment_count = fragment_chars = total_chars = 0

    def string(value, maximum):
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise PdfIrError("structure_limit")
        return str(value) if value is not None else None

    def no_inline(content):
        if content is not None and any(operator == b"INLINE IMAGE" for _, operator in content.operations):
            raise PdfIrError("unsupported_content")

    def resolved(value):
        value = value.get_object() if hasattr(value, "get_object") else value
        return None if isinstance(value, NullObject) else value

    def check_font(font):
        font = resolved(font)
        if not isinstance(font, dict) or font.get("/Subtype") == "/Type3" or "/CharProcs" in font:
            raise PdfIrError("unsupported_content")

    def check_resource_carriers(resource_dict):
        # B1 inventories ordinary image XObjects, not arbitrary rendering trees.
        # These carriers can contain images outside that inventory: fail closed,
        # rather than report a misleading zero-resource page or invent rendering.
        if resolved(resource_dict.get("/Pattern")):
            raise PdfIrError("unsupported_content")
        fonts = resolved(resource_dict.get("/Font", {})) or {}
        for font in fonts.values():
            check_font(font)
        states = resolved(resource_dict.get("/ExtGState", {})) or {}
        for state in states.values():
            state = resolved(state)
            mask = resolved(state.get("/SMask"))
            if mask is not None and mask != "/None":
                raise PdfIrError("unsupported_content")
            font = resolved(state.get("/Font"))
            if font is not None:
                if not isinstance(font, list) or len(font) != 2:
                    raise PdfIrError("unsupported_content")
                check_font(font[0])

    def check_annotation_appearances(page):
        for reference in resolved(page.get("/Annots", [])) or []:
            annotation = resolved(reference)
            if resolved(annotation.get("/AP")):
                raise PdfIrError("unsupported_content")

    def resource_for(reference, obj, key):
        nonlocal resource_bytes
        if key in resource_index:
            return resource_index[key]
        if len(resources) >= limits.max_resources:
            raise PdfIrError("resource_limit")
        if any(name in obj for name in ("/F", "/FFilter", "/FDecodeParms")):
            raise PdfIrError("unsupported_content")
        data = getattr(obj, "_data", None)
        if not isinstance(data, bytes):
            raise PdfIrError("unsupported_content")
        resource_bytes += len(data)
        if len(data) > limits.max_resource_bytes or resource_bytes > limits.max_total_resource_bytes:
            raise PdfIrError("resource_limit")
        filters = obj.get("/Filter", [])
        filters = [filters] if isinstance(filters, str) else filters
        if not isinstance(filters, list) or len(filters) > 10 or any(not isinstance(x, str) or not x.startswith("/") or len(x) > 128 for x in filters):
            raise PdfIrError("unsupported_content")
        filters = list(map(str, filters)); resource_flags = {"encoded_stream_only"}
        if filters not in (["/DCTDecode"], ["/JPXDecode"]):
            resource_flags.add("opaque_filter")
        if "/Mask" in obj or "/SMask" in obj or getattr(obj.get("/ImageMask"), "value", False) is True:
            resource_flags.add("mask_dependency_present")
        color_space = obj.get("/ColorSpace")
        if color_space is not None and str(color_space) not in {"/DeviceRGB", "/DeviceGray", "/DeviceCMYK"}:
            resource_flags.add("color_space_dependency_present")
        if "/DecodeParms" in obj or "/Decode" in obj:
            resource_flags.add("decode_parameters_present")
        resource_id = _resource_id(digest, len(resources) + 1)
        item = {"resource_id": resource_id, "object_id": [reference.idnum, reference.generation] if hasattr(reference, "idnum") else None,
                "filters": filters, "encoded_stream_base64": base64.b64encode(data).decode("ascii"),
                "encoded_stream_sha256": hashlib.sha256(data).hexdigest(), "encoded_size": len(data),
                "flags": _ordered(resource_flags, _RESOURCE_FLAGS)}
        for name, pdf_name in (("width", "/Width"), ("height", "/Height"), ("bits_per_component", "/BitsPerComponent")):
            value = obj.get(pdf_name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 1_000_000_000):
                raise PdfIrError("unsupported_content")
            item[name] = int(value) if value is not None else None
        resources.append(item); resource_index[key] = resource_id
        return resource_id

    for number, page in enumerate(reader.pages, 1):
        warning_before = counter.count
        page_flags, refs, seen = set(), [], set()
        def walk(resource_dict, route, depth=0):
            if depth > 20:
                raise PdfIrError("structure_limit")
            if not resource_dict:
                return
            resource_dict = resource_dict.get_object() if hasattr(resource_dict, "get_object") else resource_dict
            check_resource_carriers(resource_dict)
            objects = resource_dict.get("/XObject", {})
            objects = objects.get_object() if hasattr(objects, "get_object") else objects
            for name, reference in objects.items():
                key = ("object", reference.idnum, reference.generation) if hasattr(reference, "idnum") else ("direct", number, route + "/" + str(name))
                if key in seen:
                    continue
                seen.add(key)
                if len(seen) > 20000:
                    raise PdfIrError("structure_limit")
                obj = reference.get_object()
                if obj.get("/Subtype") == "/Image":
                    refs.append(resource_for(reference, obj, key))
                elif obj.get("/Subtype") == "/Form":
                    if any(name in obj for name in ("/Ref", "/F", "/FFilter", "/FDecodeParms")):
                        raise PdfIrError("unsupported_content")
                    page_flags.add("nested_forms")
                    no_inline(ContentStream(obj, reader))
                    walk(obj.get("/Resources", {}), route + "/" + str(name), depth + 1)
                else:
                    raise PdfIrError("unsupported_content")
        check_annotation_appearances(page)
        walk(page.get("/Resources", {}), "page")
        content = page.get_contents()
        no_inline(content)
        fragments, page_fragment_chars = [], 0
        def visitor(text, cm, tm, font, size):
            nonlocal fragment_count, fragment_chars, page_fragment_chars
            if not text:
                return
            if type(text) is not str:
                raise PdfIrError("corrupt_pdf")
            fragment_count += 1; fragment_chars += len(text); page_fragment_chars += len(text)
            if (len(fragments) >= limits.max_fragments_per_page or fragment_count > limits.max_total_fragments
                    or page_fragment_chars > limits.max_page_chars or fragment_chars > limits.max_total_chars):
                raise PdfIrError("fragment_limit")
            def matrix(values):
                try:
                    result = [float(x) for x in values]
                    if len(result) != 6 or not all(_finite(x) for x in result):
                        raise ValueError()
                    if result[1] != 0 or result[2] != 0:
                        page_flags.add("complex_geometry")
                    return result
                except (ValueError, TypeError, OverflowError):
                    page_flags.add("complex_geometry")
                    return None
            try:
                font_size = float(size)
                if not _finite(font_size):
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                font_size = None; page_flags.add("complex_geometry")
            sequence = len(fragments) + 1
            fragments.append({"fragment_id": _page_id(digest, number) + f":f{sequence:06d}",
                              "sequence": sequence, "text": text, "cm": matrix(cm), "tm": matrix(tm),
                              "font_size": font_size, "font_name": string(font.get("/BaseFont") if font else None, 1024)})
        text = page.extract_text(visitor_text=visitor)
        if type(text) is not str:
            raise PdfIrError("corrupt_pdf")
        total_chars += len(text)
        if len(text) > limits.max_page_chars or total_chars > limits.max_total_chars:
            raise PdfIrError("text_limit")
        page_flags.update(_text_flags(text))
        if "".join(fragment["text"] for fragment in fragments) != text:
            page_flags.add("fragment_text_mismatch")
        if counter.count > warning_before:
            page_flags.add("parser_warnings")
        pages.append({"page_number": number, "page_id": _page_id(digest, number), "raw_text": text,
                      "char_count": len(text), "nonspace_count": sum(not char.isspace() for char in text),
                      "fragments": fragments, "image_resource_ids": refs,
                      "has_content_stream": page.get("/Contents") is not None, "flags": _ordered(page_flags, _PAGE_FLAGS)})
    metadata = reader.metadata or {}
    metadata = {"title": string(metadata.get("/Title"), 2048), "author": string(metadata.get("/Author"), 2048)}
    outline = []
    def outline_walk(items, depth=0):
        if depth > 20:
            raise PdfIrError("structure_limit")
        for item in items:
            if isinstance(item, list):
                outline_walk(item, depth + 1)
                continue
            if len(outline) >= 5000 or not isinstance(item, dict):
                raise PdfIrError("structure_limit")
            title = string(item.get("/Title", ""), 2048)
            try:
                destination = reader.get_destination_page_number(item)
                destination = destination + 1 if type(destination) is int and 0 <= destination < page_count else None
            except Exception:
                destination = None
            if destination is None:
                book_flags.add("outline_unresolved")
            outline.append({"title": title or "", "page_number": destination, "depth": depth})
    outline_walk(reader.outline)
    if counter.count:
        book_flags.add("parser_warnings")
    return {"schema_version": _SCHEMA, "source_sha256": digest, "parser_version": _PARSER,
            "status": "review_required", "eligible_for_payment": False, "page_count": page_count,
            "total_char_count": total_chars, "total_nonspace_count": sum(page["nonspace_count"] for page in pages),
            "total_fragment_count": fragment_count, "total_encoded_resource_bytes": resource_bytes,
            "flags": _ordered(book_flags, _BOOK_FLAGS), "metadata": metadata, "outline": outline,
            "pages": pages, "resources": resources}


def _worker_main(argv):
    if len(argv) != 4 or argv[0] != "--worker":
        return 2
    source, result = Path(argv[1]), Path(argv[2])
    try:
        limits = _validate_limits(PdfIrLimits(**json.loads(argv[3])))
        guard = _preflight()
        memory_limited = guard._child_sandbox(limits)
        try:
            payload = _parse_snapshot(source, limits, memory_limited)
        except (PdfIrError, guard.PdfPreflightError) as exc:
            payload = {"error": exc.reason}
        except Exception:
            payload = {"error": "corrupt_pdf"}
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > limits.max_result_bytes:
            data = b'{"error":"result_limit"}'
        fd = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
