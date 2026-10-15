"""Structural source-to-artifact coverage, independent of translation language.

The in-memory baseline contains positions, not manuscript text. This detects
missing/empty text units, not omitted sentences within a nonempty translation
or semantic faithfulness. It must be captured BEFORE preprocessing/translation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import posixpath
from urllib.parse import unquote, urlsplit
import zipfile

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
import ebooklib
from lxml import etree
from billiard.exceptions import SoftTimeLimitExceeded
from app.cancellation import JobCancelled

from app.engine.chunk_extractor import is_source_placeholder_document
from app.engine.unpacker import EpubUnpacker
from app.engine.epub_compat import package_path


VERSION = "content-coverage-v1"
_BLOCKS = {"body", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
           "li", "blockquote", "td", "th", "dt", "dd", "pre", "figcaption"}
_IGNORED = {"script", "style", "svg", "math", "img", "image", "head"}


def _units(raw, *, bilingual=False):
    soup = BeautifulSoup(raw, "html.parser")
    # Generated navigation is rebuilt separately. Do not exempt editorial
    # prose elsewhere in a navigation document, or citation-only footnotes.
    for node in list(soup.find_all("nav")):
        if set(str(node.get("epub:type", "")).split()) & {"toc", "page-list", "landmarks"}:
            node.decompose()
    if bilingual:
        for node in list(soup.select(".epub-original")):
            if node.parent is not None:
                node.decompose()  # The retained source cannot stand in for the translation.
    body = soup.find("body") or soup
    result = {}
    counts = defaultdict(Counter)
    stack = [(body, "", False)]
    while stack:
        node, parent_path, ignored = stack.pop()
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            if not ignored and str(node).strip():
                result[parent_path]["nonempty"] = True
            continue
        if not isinstance(node, Tag):
            continue
        ignored = ignored or node.name in _IGNORED
        if ignored:
            continue
        path = parent_path
        # StemGuard adds this scroll-only wrapper during preprocessing. It is
        # not a new prose block; keep the table's source locators stable. Only
        # the exact, text-free single-table shape is transparent.
        children = [child for child in node.children if isinstance(child, Tag)]
        table_wrapper = (node.name == "div" and "epub-table-wrap" in node.get("class", [])
                         and len(children) == 1 and children[0].name == "table"
                         and not any(str(child).strip() for child in node.children
                                     if isinstance(child, NavigableString) and not isinstance(child, Comment)))
        semantic_block = node.name in _BLOCKS and not table_wrapper
        if node is body:
            path = "/body"
        elif semantic_block:
            counts[parent_path][node.name] += 1
            path = f"{parent_path}/{node.name}[{counts[parent_path][node.name]}]"
        if node is body or semantic_block:
            result[path] = {"id": str(node.get("id") or ""), "nonempty": False}
        stack.extend((child, path, ignored) for child in reversed(list(node.children)))
    return result


def _documents(path, *, bilingual=False):
    # Use the same legacy parsing/physical-name normalization as the product.
    # Never read a generated placeholder as replacement prose for a real page.
    book = EpubUnpacker(Path(path)).load_book()
    if book is None:
        raise ValueError("unreadable_epub")
    return _book_documents(book, bilingual=bilingual)


def _book_documents(book, *, bilingual=False):
    docs = {}
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        name = item.get_name()
        if name in docs:
            raise ValueError("ambiguous_document_identity")
        raw = item.get_content()
        if raw is None:
            raise ValueError("unreadable_document")
        docs[name] = {"placeholder": is_source_placeholder_document(raw),
                      "units": _units(raw, bilingual=bilingual)}
    return docs


def capture_content_baseline(source_path, *, book=None, allow_textless=False):
    """Fail closed before model calls if the source cannot be inventoried."""
    documents = _book_documents(book) if book is not None else _documents(source_path)
    expected = {}
    skipped = 0
    for name, doc in documents.items():
        if doc["placeholder"]:
            skipped += 1
            continue
        units = {path: unit["id"] for path, unit in doc["units"].items() if unit["nonempty"]}
        if units:
            expected[name] = units
    if not expected and allow_textless:
        # Image-only publications are valid basic-conversion inputs. Still
        # require their real documents to survive; placeholders do not count.
        expected = {name: {} for name, doc in documents.items() if not doc["placeholder"]}
    if not expected:
        raise ValueError("no_source_text_units")
    return {"version": VERSION, "documents": expected, "source_placeholders_skipped": skipped}


def _output_documents(path, *, bilingual):
    # Inspect delivered bytes, never repair a missing/empty output chapter into
    # a placeholder or reject it before we can count the exact missing units.
    docs = {}
    with zipfile.ZipFile(path) as archive:
        members = set(archive.namelist())
        opf = package_path(archive)
        root = etree.fromstring(archive.read(opf), etree.XMLParser(resolve_entities=False, no_network=True))
        for item in root.findall('{*}manifest/{*}item'):
            if item.get('media-type') not in {'application/xhtml+xml', 'text/html'}:
                continue
            uri = urlsplit(item.get('href', ''))
            name = unquote(uri.path)
            member = posixpath.normpath(posixpath.join(posixpath.dirname(opf), name))
            if uri.scheme or uri.netloc or not name or member.startswith(('/', '../')) or '\\' in name:
                raise ValueError('invalid_document_identity')
            if name in docs:
                raise ValueError('ambiguous_document_identity')
            if member not in members:
                continue
            raw = archive.read(member)
            docs[name] = {'placeholder': is_source_placeholder_document(raw),
                          'units': _units(raw, bilingual=bilingual)}
    return docs


def audit_content_completeness(baseline, output_path, *, bilingual=False, sample_limit=12):
    report = {"version": VERSION, "status": "passed", "expected_documents": 0,
              "expected_units": 0, "missing_documents": 0, "missing_units": 0,
              "empty_units": 0, "samples": []}
    limit = max(0, min(int(sample_limit), 100))

    def record(name, path, reason):
        if len(report["samples"]) < limit:
            report["samples"].append({"file": name, "locator": path, "reason": reason})

    try:
        if not isinstance(baseline, dict) or baseline.get("version") != VERSION or not baseline.get("documents"):
            raise ValueError("missing_source_baseline")
        expected = baseline["documents"]
        report["expected_documents"] = len(expected)
        report["expected_units"] = sum(len(units) for units in expected.values())
        report["source_placeholders_skipped"] = baseline["source_placeholders_skipped"]
        actual = _output_documents(output_path, bilingual=bilingual)
        for name, units in expected.items():
            doc = actual.get(name)
            if doc is None or doc["placeholder"]:
                report["missing_documents"] += 1
                report["missing_units"] += len(units)
                record(name, "", "missing_document")
                continue
            by_id = defaultdict(list)
            for path, unit in doc["units"].items():
                if unit["id"]:
                    by_id[unit["id"]].append(path)
            for path, identifier in units.items():
                # An existing ID is a stronger identity than a shifted ordinal.
                candidates = by_id.get(identifier, []) if identifier else [path]
                target = doc["units"].get(candidates[0]) if len(candidates) == 1 else None
                if target is None:
                    report["missing_units"] += 1
                    record(name, path, "missing_or_ambiguous_unit")
                elif not target["nonempty"]:
                    report["empty_units"] += 1
                    record(name, path, "empty_unit")
        if report["missing_documents"] or report["missing_units"] or report["empty_units"]:
            report["status"] = "failed"
    except (SoftTimeLimitExceeded, JobCancelled):
        raise
    except Exception:
        # Do not publish parser exceptions, filesystem paths or manuscript text.
        report["status"] = "scan_error"
        report["reason"] = "无法核对原稿与成品的正文完整性"
    return report
