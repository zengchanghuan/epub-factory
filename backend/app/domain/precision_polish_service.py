"""Transactional, paid precision polish between conversion QA and publication.

This service has no payment/refund authority. Callers establish the usage ledger
scope and publish only its successfully checked, attempt-specific output.
"""
from __future__ import annotations

import os
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

from app.cancellation import raise_if_cancelled
from app.engine.cleaners.llm_polish import (
    L4Stats, LLMPolisher, PrecisionPolishError, _NUMBER, plan_document, validate_document_input_budget,
)
from app.engine.epub_validation import validate_epub
from app.infra.llm_guard import ModelNotAllowedError


EPUBCHECK_JAR = Path(os.environ.get("EPUBCHECK_JAR") or
                     Path(__file__).resolve().parents[3] / "tools" / "epubcheck-5.1.0" / "epubcheck.jar")


def _xml(content):
    return etree.fromstring(content, etree.XMLParser(resolve_entities=False, load_dtd=False,
                                                     no_network=True, recover=False))


def _resource_path(base, href, names):
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or parts.query or parts.fragment or "\\" in href:
        raise ValueError("Invalid EPUB resource reference")
    raw = unquote(parts.path, errors="strict")
    resolved = posixpath.normpath(posixpath.join(base, raw))
    if raw.startswith("/") or resolved.startswith("../") or resolved not in names:
        raise ValueError("Missing/unsafe EPUB resource")
    return resolved


def _load_plans(path):
    """Read manifest/spine without extracting files or changing the source."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = set(archive.namelist())
            if len(names) != len(entries) or archive.read("mimetype").strip() != b"application/epub+zip":
                raise ValueError("Invalid EPUB archive")
            # No extraction occurs, but ambiguous/hostile archive identities
            # should not be accepted as a billable input.
            if any(n.startswith("/") or "\\" in n or ".." in n.split("/") for n in names):
                raise ValueError("Unsafe ZIP member")
            container = _xml(archive.read("META-INF/container.xml"))
            roots = container.xpath("//*[local-name()='rootfile']/@full-path")
            if len(roots) != 1 or roots[0] not in names:
                raise ValueError("Ambiguous EPUB package")
            package_path = roots[0]
            package = _xml(archive.read(package_path))
            items = {}
            for item in package.xpath("//*[local-name()='manifest']/*[local-name()='item']"):
                identity = item.get("id")
                if not identity or identity in items:
                    raise ValueError("Invalid manifest identity")
                items[identity] = item
            plans = {}
            for ref in package.xpath("//*[local-name()='spine']/*[local-name()='itemref']"):
                item = items.get(ref.get("idref"))
                if item is None:
                    raise ValueError("Missing spine item")
                if item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
                    continue
                if "nav" in item.get("properties", "").split():
                    continue
                name = _resource_path(posixpath.dirname(package_path), item.get("href", ""), names)
                if name not in plans:
                    plans[name] = plan_document(archive.read(name))
            if not plans or not sum(plan.char_count for plan in plans.values()):
                raise PrecisionPolishError("no_body_text", "书稿没有可精校的正文文字，未发起模型请求")
            return plans
    except PrecisionPolishError:
        raise
    except (OSError, KeyError, ValueError, UnicodeError, etree.XMLSyntaxError, zipfile.BadZipFile, RuntimeError) as exc:
        raise PrecisionPolishError("invalid_source", "无法安全读取 EPUB 正文，未发起模型请求") from exc


def inspect_precision_polish_source(epub_path: Path, *, traditional_variant: str = "auto",
                                    lexicon_domains: list[str] | None = None,
                                    enable_proper_noun: bool = True) -> dict:
    """Quote the source text, but review candidates after the selected L1–L3.

    The actual conversion applies the same CjkNormalizer before paid review.
    A term already resolved by its dictionaries (e.g. 超商→便利店) must not
    cause a new paid order that inevitably reaches no_candidates. No archive
    is written here; execution receives an already converted artifact and does
    not use this inspection normalization a second time.
    """
    from app.engine.cleaners.cjk_normalizer import CjkNormalizer

    original_plans = _load_plans(Path(epub_path))
    char_count = sum(p.char_count for p in original_plans.values())
    cleaner = CjkNormalizer(output_mode="simplified", traditional_variant=traditional_variant,
                            lexicon_domains=lexicon_domains, enable_proper_noun=enable_proper_noun)
    # The real unpacker/ebooklib boundary resolves numeric character entities
    # before cleaners run. Use the already validated XML tree, not the original
    # lexical spelling (e.g. &#x8d85;&#x5546;), so dictionaries see the same words.
    plans = [plan_document(cleaner.process(
        etree.tostring(plan.tree, encoding="utf-8", xml_declaration=True), 9))
        for plan in original_plans.values()]
    for plan in plans:
        validate_document_input_budget(plan)
    return {"char_count": char_count,
            "candidates": sum(len(p.paragraphs) for p in plans),
            "documents_scanned": len(plans),
            "paragraphs_scanned": sum(p.paragraphs_scanned for p in plans)}


def _snapshot(plan):
    nodes = list(plan.tree.getroot().iter())
    identities = {node: index for index, node in enumerate(nodes)}
    allowed = {}
    for paragraph in plan.paragraphs:
        for occurrence in paragraph.occurrences:
            if occurrence.editable:
                allowed.setdefault((identities[occurrence.node], occurrence.field), []).append(occurrence)
    records = []
    for index, node in enumerate(nodes):
        tag = node.tag if isinstance(node.tag, str) else type(node).__name__
        fields = []
        for field in ("text", "tail"):
            original = getattr(node, field) or ""
            pieces, position = [], 0
            for occurrence in sorted(allowed.get((index, field), []), key=lambda o: o.start):
                pieces.extend((re.escape(original[position:occurrence.start]), r"[\u3400-\u9fff]{1,24}"))
                position = occurrence.end
            pattern = "".join(pieces) + re.escape(original[position:]) if pieces else None
            fields.append((original, pattern))
        records.append((tag, tuple(sorted(node.attrib.items())), node.getparent() is None,
                        len(node), fields))
    return records, plan.tree.docinfo.doctype


def _check_document(snapshot, after):
    try:
        root = _xml(after)
        nodes = list(root.iter())
        records, doctype = snapshot
        if len(nodes) != len(records) or root.getroottree().docinfo.doctype != doctype:
            raise ValueError("XML structure changed")
        for node, (tag, attrs, is_root, children, fields) in zip(nodes, records):
            actual_tag = node.tag if isinstance(node.tag, str) else type(node).__name__
            if (actual_tag != tag or tuple(sorted(node.attrib.items())) != attrs
                    or (node.getparent() is None) != is_root or len(node) != children):
                raise ValueError("XML structure changed")
            for field, (original, pattern) in zip(("text", "tail"), fields):
                current = getattr(node, field) or ""
                if current != original and (not pattern or not re.fullmatch(pattern, current)
                                            or _NUMBER.findall(current) != _NUMBER.findall(original)):
                    raise ValueError("Protected text changed")
    except (etree.XMLSyntaxError, ValueError, TypeError) as exc:
        raise PrecisionPolishError("guard_rejected", "精校成品改变了结构或受保护文字，已停止交付") from exc


def run_precision_polish(input_path: Path, output_path: Path, *, cancel_check=None,
                         stats_callback=None, polisher_factory=LLMPolisher) -> dict:
    """No output is published unless every candidate and final QA succeeds.

    Factory contract: no arguments, context manager yielding LLMPolisher (or a
    subclass). Cancellation is injected before use; accounting remains external.
    Callback receives numeric/state metadata only, never a book excerpt.
    """
    source, destination = Path(input_path), Path(output_path)
    if source.resolve() == destination.resolve() or destination.exists() or destination.is_symlink():
        raise PrecisionPolishError("invalid_destination", "精校输出必须使用未占用的独立临时路径")
    check = lambda: raise_if_cancelled(cancel_check, "用户已停止精校")
    callback = stats_callback or (lambda stats: None)
    stats = L4Stats()
    temporary = None
    published = False

    def report():
        callback(stats.to_dict())

    try:
        check()
        plans = _load_plans(source)
        stats.documents_scanned = len(plans)
        stats.paragraphs_scanned = sum(p.paragraphs_scanned for p in plans.values())
        stats.candidates = sum(len(p.paragraphs) for p in plans.values())
        report()
        for plan in plans.values():
            validate_document_input_budget(plan)
        replacements = {}
        if stats.candidates:
            with polisher_factory() as polisher:
                polisher.cancel_check = check
                polisher.stats_callback = lambda snapshot: report()
                polisher.stats = stats
                stats.model = polisher.model
                stats.provider = urlsplit(polisher.base_url).hostname or "unknown"
                for name, plan in plans.items():
                    check()
                    before = _snapshot(plan)
                    updated = polisher.polish_document(plan)
                    _check_document(before, updated)
                    if updated != plan.source:
                        replacements[name] = updated
                    report()
        if stats.reviewed != stats.candidates or stats.changed + stats.unchanged != stats.reviewed:
            raise PrecisionPolishError("incomplete_review", "存在尚未完成审阅的风险段，已停止交付")
        check()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix=".precision-", suffix=".epub", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(filename)
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(temporary, "w") as output:
            output.comment = original.comment
            for entry in original.infolist():
                content = replacements.get(entry.filename)
                output.writestr(entry, original.read(entry.filename) if content is None else content)
        # Verify the actual archive, not just in-memory HTML. All other members
        # (including OPF/NCX/nav/images/fonts/CSS) must remain byte-identical.
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(temporary) as output:
            if original.namelist() != output.namelist():
                raise PrecisionPolishError("guard_rejected", "精校改动了资源清单")
            for name in original.namelist():
                expected = replacements.get(name, original.read(name))
                if output.read(name) != expected:
                    raise PrecisionPolishError("guard_rejected", "精校改动了受保护资源")
        check()
        validation = validate_epub(temporary, EPUBCHECK_JAR)
        if not validation.passed:
            raise PrecisionPolishError("validation_failed", validation.message)
        check()
        stats.validation_passed = True
        stats.status = "completed" if stats.candidates else "no_candidates"
        # The runner supplies an attempt-specific destination and is responsible
        # for lease-aware publication to the public download path.
        os.replace(temporary, destination)
        published = True
        report()
        return stats.to_dict()
    except (PrecisionPolishError, ModelNotAllowedError) as exc:
        stats.status = "failed"
        stats.failed = max(1, stats.candidates - stats.reviewed)
        stats.reason = exc.reason if isinstance(exc, PrecisionPolishError) else "model_not_allowed"
        stats.refund_required = True  # Review required, never an assertion of refund.
        stats.validation_passed = False
        error = PrecisionPolishError(stats.reason, str(exc), stats.to_dict())
        report()
        raise error from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        # A failing callback/cancellation/accounting exception must not leave a
        # success-looking file either. BaseException (AccountingError) propagates.
        import sys
        if published and sys.exc_info()[0] is not None:
            destination.unlink(missing_ok=True)
