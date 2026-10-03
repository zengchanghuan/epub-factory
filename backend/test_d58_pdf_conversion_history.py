"""Opt-in, independent B2 conversion audit of the user's real Chinese PDF.

The source is reparsed with pdfplumber and pypdf, not a saved IR or the adapter's
own export helpers. Every page, text sequence, image draw/object, JPEG byte
stream, decoded RGB/alpha plane and original TOC target is checked against the
generated EPUB. This is one-book structural/content acceptance, not proof of
general PDF reading order, OCR, translation quality or visual reader behavior.

Default conversion MUST retain the four advertising overlays. Their removal
was authorized only for an earlier manual artifact, not for this product path.
All book-specific facts, paragraph examples and SHA pins belong in this test.

Run separately from release_guard: the actual parser intentionally uses its
own isolated child. EPUB_PDF_HISTORY_FILE must point to the selected original;
discovery skips without it, while an explicit executable gate refuses to pass
without a real sample. No customer file is copied into the repository.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import ExitStack
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile


SOURCE_ENV = "EPUB_PDF_HISTORY_FILE"
SOURCE_SHA256 = "af21894c7542c1a7e3c286a762cc15c1d1aeaa81fb9ca8e028d85ff5d22b5023"
SOURCE_SIZE = 7020179
PAGE_COUNT = 346
NORMALIZED_CHARACTERS = 167334
IMAGE_DRAWS = 111
IMAGE_OBJECTS = 110
SOFT_MASKS = 42
IMAGE_ONLY_PAGES = (1, 3, 5, 9, 207, 346)
AD_PAGES = (159, 177, 209, 271)
ADVERTISEMENT = "代找各类书籍5元/本，需要的加微：lhldxxzl  备用微：zfnztp"
EPUB_NS = "http://www.idpf.org/2007/ops"
PAGE_ATTRIBUTE = "data-pdf-page"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def file_identity(path):
    value = Path(path).stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode)


def normalized(text):
    """Whitespace/U+200B only: punctuation, Latin symbols and accents survive."""
    return "".join(c for c in text if not c.isspace() and c != "\u200b")


def require(condition, reason):
    # Do not include source text, parser exceptions or local paths in failures.
    if not condition:
        raise AssertionError(reason)


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def object_identity(value):
    match = re.fullmatch(r"([0-9]+):([0-9]+)", str(value))
    require(match is not None, "Image mapping must contain source object id:generation")
    return int(match.group(1)), int(match.group(2))


def css_declarations(value):
    """Inspect emitted CSS, not diagnostic data attributes or adapter receipts."""
    result = {}
    for declaration in value.split(";"):
        if not declaration.strip():
            continue
        require(":" in declaration, "Malformed generated CSS declaration")
        key, item = declaration.split(":", 1)
        require("!important" not in item, "Unexpected generated CSS priority override")
        result[key.strip()] = item.strip()
    return result


def linked_css_rule(snapshot, document, wanted_selector):
    """Resolve the generated stylesheet actually linked by this image's XHTML."""
    matches = []
    for link in snapshot.docs[document].iter():
        if local_name(link.tag) != "link" or "stylesheet" not in link.get("rel", "").split():
            continue
        path, fragment = internal_target(document, link.get("href", ""))
        require(not fragment and path in snapshot.resource_bytes, "Missing image stylesheet")
        css = snapshot.resource_bytes[path].decode("utf-8")
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if wanted_selector in [part.strip() for part in selectors.split(",")]:
                matches.append(css_declarations(declarations))
    require(len(matches) == 1, "Image rendering rule absent or ambiguously overridden")
    return matches[0]


def internal_target(document, value):
    parts = urlsplit(value)
    require(not parts.scheme and not parts.netloc and not parts.query, "External resource reference")
    raw = unquote(parts.path)
    require(not raw.startswith("/") and "\\" not in raw and "\x00" not in raw, "Unsafe resource reference")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(document), raw)) if raw else document
    require(target != ".." and not target.startswith("../"), "Escaping resource reference")
    return target, unquote(parts.fragment)


def mapped_source_prefix(snapshot, document, target):
    """Locate an anchor in the preserved source character stream, not by its ID."""
    span = target
    while span is not None and PAGE_ATTRIBUTE not in span.attrib:
        span = snapshot.parents[document].get(span)
    require(span is not None and span.get("class") != "pagebreak", "Link is not anchored within source text")
    page = int(span.attrib[PAGE_ATTRIBUTE])
    previous = []
    for candidate in snapshot.page_text_nodes[page]:
        if candidate is span:
            break
        previous.append("".join(candidate.itertext()))
    else:
        require(False, "Source span missing from spine order")

    def before(node):
        if node is target:
            return True
        previous.append(node.text or "")
        for child in node:
            if before(child):
                return True
            previous.append(child.tail or "")
        return False

    require(before(span), "Anchor not present in its source span")
    return page, "".join(previous)


def inspect_source(path):
    """Independent parser/image oracle: no production conversion imports."""
    import pdfplumber
    from PIL import Image
    from pypdf import PdfReader

    before = file_identity(path)
    require(file_digest(path) == SOURCE_SHA256 and before[2] == SOURCE_SIZE, "Wrong historical source")
    reader = PdfReader(str(path), strict=True)
    if reader.is_encrypted:
        require(reader.decrypt("") in (1, 2), "Historical empty-password opening failed")
        require(reader.user_access_permissions is not None and int(reader.user_access_permissions) & 16,
                "Historical source does not explicitly permit extraction")
    require(len(reader.pages) == PAGE_COUNT, "Historical source page count changed")
    resources, owner_refs, outlines, annotations = {}, Counter(), [], []
    page_references = {(page.indirect_reference.idnum, page.indirect_reference.generation): number
                       for number, page in enumerate(reader.pages, 1)}

    def walk_outline(entries, depth=0):
        for entry in entries:
            if isinstance(entry, list):
                walk_outline(entry, depth + 1)
            else:
                number = reader.get_destination_page_number(entry) + 1
                left, _, _, top = map(float, reader.pages[number - 1].mediabox)
                point = None
                if entry.get("/Type") == "/XYZ" and isinstance(entry.get("/Left"), (int, float)) and isinstance(entry.get("/Top"), (int, float)):
                    point = (float(entry["/Left"]) - left, top - float(entry["/Top"]))
                outlines.append({"title": entry.title, "page": number, "depth": depth, "target_point": point})

    walk_outline(reader.outline)
    require(len(outlines) == 28, "Historical outline count changed")
    pypdf_text = []
    for page_number, page in enumerate(reader.pages, 1):
        pypdf_text.append(page.extract_text() or "")
        for reference in page.get("/Annots", []):
            annotation = reference.get_object()
            destination = annotation.get("/Dest")
            require(annotation.get("/Subtype") == "/Link" and "/A" not in annotation
                    and isinstance(destination, list) and len(destination) == 5 and destination[1] == "/XYZ",
                    "Historical annotation contract changed")
            target_page = page_references[(destination[0].idnum, destination[0].generation)]
            left, _, _, top = map(float, page.mediabox)
            target_left, _, _, target_top = map(float, reader.pages[target_page - 1].mediabox)
            x0, y0, x1, y1 = map(float, annotation["/Rect"])
            annotations.append({"id": str(len(annotations) + 1), "source_page": page_number,
                                "target_page": target_page,
                                "rect": (x0 - left, top - y1, x1 - left, top - y0),
                                "target_point": (float(destination[2]) - target_left,
                                                 target_top - float(destination[3]))})
        objects = page.get("/Resources", {}).get("/XObject", {})
        for ref in objects.values():
            identity = (int(ref.idnum), int(ref.generation))
            image = ref.get_object()
            require(image.get("/Subtype") == "/Image", "Unexpected historical resource carrier")
            owner_refs[page_number, identity] += 1
            if identity in resources:
                continue
            encoded = image._data
            encoding = image.get("/Filter")
            if isinstance(encoding, list):
                require(len(encoding) == 1, "Historical base image filter chain changed")
                encoding = encoding[0]
            require(encoding == "/DCTDecode" and image.get("/ColorSpace") == "/DeviceRGB"
                    and image.get("/BitsPerComponent") == 8, "Historical base image interpretation changed")
            size = (int(image["/Width"]), int(image["/Height"]))
            with Image.open(BytesIO(encoded)) as base:
                base.load()
                require(base.format == "JPEG" and base.mode == "RGB" and base.size == size,
                        "Historical JPEG dictionary/sample mismatch")
                rgb_hash = digest(base.tobytes())
            alpha_hash = None
            if "/SMask" in image:
                mask = image["/SMask"]
                require(mask.get("/ColorSpace") == "/DeviceGray" and mask.get("/BitsPerComponent") == 8
                        and (int(mask["/Width"]), int(mask["/Height"])) == size,
                        "Historical soft mask interpretation changed")
                alpha = mask.get_data()
                require(len(alpha) == size[0] * size[1], "Historical alpha plane length changed")
                alpha_hash = digest(alpha)
            resources[identity] = {"size": size, "encoded_sha256": digest(encoded), "encoded_bytes": len(encoded),
                                   "rgb_sha256": rgb_hash, "alpha_sha256": alpha_hash}

    texts, glyphs, page_boxes, draw_refs, draw_boxes = [], [], [], Counter(), []
    with pdfplumber.open(path, password="") as book:
        require(len(book.pages) == PAGE_COUNT, "Independent parser page count disagrees")
        for page_number, page in enumerate(book.pages, 1):
            text = "".join(c["text"] for c in page.chars)
            require(normalized(text) == normalized(pypdf_text[page_number - 1]),
                    "Independent source text parsers disagree on page " + str(page_number))
            if page_number in AD_PAGES:
                require(text.endswith(ADVERTISEMENT) and text.count(ADVERTISEMENT) == 1,
                        "Authorized historical overlay fixture changed")
            texts.append(text)
            glyphs.append([{key: char[key] for key in ("text", "x0", "x1", "top", "bottom")}
                           for char in page.chars])
            page_boxes.append((page.width, page.height))
            for image in page.images:
                obj_id = int(image["stream"].objid)
                candidates = [identity for identity in resources if identity[0] == obj_id]
                require(len(candidates) == 1, "Historical image draw object is ambiguous")
                identity = candidates[0]
                draw_refs[page_number, identity] += 1
                draw_boxes.append({"page": page_number, "object": identity,
                                   "bbox": (image["x0"], image["top"], image["x1"], image["bottom"])})
            page.close()
    require(sum(len(normalized(t)) for t in texts) == NORMALIZED_CHARACTERS, "Source character baseline changed")
    require(tuple(n for n, t in enumerate(texts, 1) if not normalized(t)) == IMAGE_ONLY_PAGES,
            "Historical image-only page set changed")
    require(len(resources) == IMAGE_OBJECTS and sum(draw_refs.values()) == IMAGE_DRAWS,
            "Historical image object/draw baseline changed")
    require(sum(r["alpha_sha256"] is not None for r in resources.values()) == SOFT_MASKS,
            "Historical alpha-mask baseline changed")
    require(draw_refs == owner_refs, "Historical resource/draw baseline differs")
    require(len(annotations) == 29 and Counter(item["source_page"] for item in annotations)
            == Counter({2: 27, 28: 1, 82: 1}), "Historical internal-link baseline changed")
    for item in annotations:
        source_glyphs = glyphs[item["source_page"] - 1]
        x0, y0, x1, y1 = item["rect"]
        selected = [index for index, char in enumerate(source_glyphs) if normalized(char["text"])
                    and x0 <= (char["x0"] + char["x1"]) / 2 <= x1
                    and y0 <= (char["top"] + char["bottom"]) / 2 <= y1]
        require(bool(selected), "Historical link rectangle does not select source glyphs")
        start, end = min(selected), max(selected)
        require([index for index in range(start, end + 1) if normalized(source_glyphs[index]["text"])]
                == selected, "Historical link rectangle selects discontiguous glyphs")
        item["source_prefix"] = "".join(char["text"] for char in source_glyphs[:start])
        item["source_text"] = "".join(char["text"] for char in source_glyphs[start:end + 1])
        headings = [heading for heading in outlines if heading["page"] == item["target_page"]
                    and normalized(heading["title"]) == normalized(item["source_text"])]
        if headings:
            require(len(headings) == 1 and item["source_page"] == 2, "Ambiguous printed-TOC destination")
            require(headings[0]["target_point"] is not None and all(
                abs(a - b) < 1e-6 for a, b in zip(headings[0]["target_point"], item["target_point"])),
                "Printed-TOC XYZ differs from the original bookmark destination")
            item["target_kind"] = "heading"
        else:
            # Resolve the actual XYZ destination independently, using nearest
            # glyph origin rather than the adapter's matching implementation.
            x, y = item["target_point"]
            candidates = sorted((math.hypot(char["x0"] - x, char["top"] - y), index, char)
                                for index, char in enumerate(glyphs[item["target_page"] - 1])
                                if normalized(char["text"]))
            require(len(candidates) >= 2 and candidates[0][0] < 2.0
                    and candidates[1][0] - candidates[0][0] > 5.0,
                    "Historical XYZ destination is not a unique glyph")
            _, position, char = candidates[0]
            item.update(target_kind="glyph", target_index=position, target_text=char["text"],
                        target_prefix="".join(c["text"] for c in glyphs[item["target_page"] - 1][:position]))
    require(Counter(item["target_kind"] for item in annotations) == Counter(heading=27, glyph=2),
            "Historical link destination kinds changed")
    require(file_identity(path) == before and file_digest(path) == SOURCE_SHA256, "Source changed during independent audit")
    return {"texts": texts, "resources": resources, "draw_refs": draw_refs, "draw_boxes": draw_boxes,
            "outlines": outlines, "annotations": annotations, "page_boxes": page_boxes,
            "zero_width_count": sum(t.count("\u200b") for t in texts)}


class EpubSnapshot:
    """Inspect the emitted ZIP directly; never trust the adapter's summary."""
    def __init__(self, path):
        self.pages, self.pagebreaks = defaultdict(list), defaultdict(list)
        self.marker_order, self.paragraphs, self.toc, self.images = [], [], [], []
        self.text_blocks = []
        self.page_text_nodes = defaultdict(list)
        self.internal_links, self.spine_documents, self.cover_images = [], [], set()
        self.unmapped = []
        self.first_content = {}
        self.resource_bytes, self.docs, self.ids, self.parents = {}, {}, {}, {}
        self.references = []
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            require(len(names) == len(set(names)), "Duplicate ZIP member")
            require(infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
                    and archive.read("mimetype") == b"application/epub+zip", "Invalid EPUB mimetype")
            for info in infos:
                require(not info.filename.startswith("/") and ".." not in PurePosixPath(info.filename).parts
                        and "\\" not in info.filename and not stat.S_ISLNK(info.external_attr >> 16), "Unsafe ZIP member")
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfiles = container.findall("{*}rootfiles/{*}rootfile")
            require(len(rootfiles) == 1, "Unexpected package rootfile count")
            package_path = rootfiles[0].attrib["full-path"]
            package = ET.fromstring(archive.read(package_path))
            item_rows = package.findall("{*}manifest/{*}item")
            items = {item.attrib["id"]: item for item in item_rows}
            require(len(items) == len(item_rows), "Duplicate manifest identity")
            item_paths, nav_paths, self.manifest_images = {}, [], set()
            for item_id, item in items.items():
                target, fragment = internal_target(package_path, item.attrib["href"])
                require(target in names and not fragment, "Missing manifest resource")
                require(target not in item_paths.values(), "Duplicate manifest resource path")
                item_paths[item_id] = target
                data = archive.read(target)
                self.resource_bytes[target] = data
                media = item.attrib["media-type"]
                if media.startswith("image/"):
                    self.manifest_images.add(target)
                    if "cover-image" in item.get("properties", "").split():
                        self.cover_images.add(target)
                if media in {"application/xhtml+xml", "application/x-dtbncx+xml", "image/svg+xml"}:
                    require(b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(), "External XML declaration")
                    tree = ET.fromstring(data)
                    self.docs[target] = tree
                    self.parents[target] = {child: node for node in tree.iter() for child in node}
                    identifiers = [n.attrib["id"] for n in tree.iter() if "id" in n.attrib]
                    require(len(identifiers) == len(set(identifiers)), "Duplicate XHTML id")
                    self.ids[target] = {n.attrib["id"]: n for n in tree.iter() if "id" in n.attrib}
                    for node in tree.iter():
                        require(local_name(node.tag) not in {"script", "iframe", "object", "embed"}, "Active EPUB content")
                        for key, value in node.attrib.items():
                            if local_name(key) in {"href", "src", "poster"}:
                                self.references.append((target, value))
                            require(not local_name(key).lower().startswith("on"), "Inline script event")
                        self._css_references(target, node.attrib.get("style", ""))
                    if "nav" in item.attrib.get("properties", "").split():
                        nav_paths.append(target)
                elif media == "text/css":
                    self._css_references(target, data.decode("utf-8"))
            for origin, reference in self.references:
                target, fragment = internal_target(origin, reference)
                require(target in names, "Broken EPUB resource reference")
                require(not fragment or target in self.ids and fragment in self.ids[target], "Broken EPUB fragment")
            spine = [n.attrib["idref"] for n in package.findall("{*}spine/{*}itemref")]
            require(len(spine) == len(set(spine)), "Duplicate spine item")
            for item_id in spine:
                document = item_paths[item_id]
                if document in nav_paths:
                    continue
                self.spine_documents.append(document)
                body = self.docs[document].find("{*}body")
                require(body is not None, "Spine XHTML has no body")
                self._mapped_text(body)
                for node in body.iter():
                    if "data-pdf-link-id" in node.attrib:
                        self.internal_links.append({"node": node, "document": document})
                    if PAGE_ATTRIBUTE in node.attrib:
                        page = int(node.attrib[PAGE_ATTRIBUTE])
                        self.marker_order.append(page)
                        if normalized("".join(node.itertext())):
                            self.first_content.setdefault(page, document)
                    if "pagebreak" in node.attrib.get("{%s}type" % EPUB_NS, "").split():
                        require(PAGE_ATTRIBUTE in node.attrib and not normalized("".join(node.itertext())), "Invalid pagebreak")
                        self.pagebreaks[int(node.attrib[PAGE_ATTRIBUTE])].append((document, node.attrib.get("id")))
                    if local_name(node.tag) == "img":
                        require("data-pdf-object" in node.attrib and "data-pdf-page-image" in node.attrib,
                                "Image lost source identity")
                        page = int(node.attrib["data-pdf-page-image"])
                        identity = object_identity(node.attrib["data-pdf-object"])
                        target, fragment = internal_target(document, node.attrib["src"])
                        require(not fragment, "Unexpected image fragment")
                        self.images.append({"page": page, "object": identity, "path": target,
                                            "document": document, "node": node})
                        self.first_content.setdefault(page, document)
                    if local_name(node.tag) in {"p", "h1", "h2", "h3", "h4", "h5", "h6"}:
                        spans = [(int(n.attrib[PAGE_ATTRIBUTE]), "".join(n.itertext()))
                                 for n in node.iter() if PAGE_ATTRIBUTE in n.attrib]
                        block = {"document": document, "node": node, "spans": spans,
                                 "text": "".join(node.itertext())}
                        self.text_blocks.append(block)
                        if local_name(node.tag) == "p":
                            self.paragraphs.append(block)
            require(len(nav_paths) == 1, "Unexpected EPUB nav count")
            nav_path = nav_paths[0]
            navs = [n for n in self.docs[nav_path].iter() if local_name(n.tag) == "nav"
                    and "toc" in n.attrib.get("{%s}type" % EPUB_NS, "").split()]
            require(len(navs) == 1, "Unexpected TOC navigation count")
            for anchor in navs[0].iter():
                if local_name(anchor.tag) != "a":
                    continue
                target_path, fragment = internal_target(nav_path, anchor.attrib["href"])
                target = self.ids.get(target_path, {}).get(fragment) if fragment else self.docs[target_path].find("{*}body")
                require(target is not None, "Missing TOC target")
                nodes = list(target.iter())
                target_page = next((int(n.attrib[PAGE_ATTRIBUTE]) for n in nodes if PAGE_ATTRIBUTE in n.attrib), None)
                if target_page is None:
                    all_nodes = list(self.docs[target_path].iter())
                    start = all_nodes.index(target)
                    target_page = next((int(n.attrib[PAGE_ATTRIBUTE]) for n in all_nodes[start:] if PAGE_ATTRIBUTE in n.attrib), None)
                depth, parent = -1, self.parents[nav_path].get(anchor)
                while parent is not None and parent is not navs[0]:
                    if local_name(parent.tag) == "li":
                        depth += 1
                    parent = self.parents[nav_path].get(parent)
                self.toc.append({"title": "".join(anchor.itertext()), "page": target_page, "depth": depth,
                                 "target_tag": local_name(target.tag), "target_text": "".join(target.itertext())})

    def _css_references(self, document, css):
        for value in re.findall(r"url\(\s*[\"']?([^\)\"']+)", css):
            self.references.append((document, value.strip()))
        for value in re.findall(r"@import\s+[\"']([^\"']+)", css):
            self.references.append((document, value))

    def _mapped_text(self, node):
        if PAGE_ATTRIBUTE in node.attrib:
            require(not any(PAGE_ATTRIBUTE in child.attrib for child in list(node.iter())[1:]), "Nested source-page text mapping")
            require("hidden" not in node.attrib and node.attrib.get("aria-hidden") != "true", "Source text hidden from readers")
            self.pages[int(node.attrib[PAGE_ATTRIBUTE])].append("".join(node.itertext()))
            self.page_text_nodes[int(node.attrib[PAGE_ATTRIBUTE])].append(node)
            return
        self.unmapped.append(node.text or "")
        for child in node:
            self._mapped_text(child)
            self.unmapped.append(child.tail or "")


class PdfConversionHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get(SOURCE_ENV, "").strip()
        if not configured:
            raise unittest.SkipTest("EPUB_PDF_HISTORY_FILE not provided; real PDF conversion gate not executed")
        cls.path = Path(configured).absolute()
        require(cls.path.is_file() and file_digest(cls.path) == SOURCE_SHA256, "Explicit SHA-pinned real PDF required")
        cls.original = file_identity(cls.path)
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="d58-real-conversion-"))).resolve()
        selected = {key: value for key, value in os.environ.items() if key in {"PATH", "JAVA_HOME", "EPUBCHECK_JAR"}}
        selected.update({SOURCE_ENV: str(cls.path), "HOME": str(cls.root), "TMPDIR": str(cls.root),
                         "TMP": str(cls.root), "TEMP": str(cls.root), "PYTHONDONTWRITEBYTECODE": "1",
                         "DATABASE_URL": "sqlite:///" + str(cls.root / "unused.db"),
                         "OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "", "DASHSCOPE_API_KEY": "", "GEMINI_API_KEY": "",
                         "ALIPAY_APP_ID": "", "ALIPAY_PRIVATE_KEY": "", "ALIPAY_PUBLIC_KEY": "", "SMTP_HOST": "",
                         "SENTRY_DSN": "", "NOTIFY_EMAIL_ENABLED": "0", "OWNER_PAYMENT_EMAIL_ENABLED": "0",
                         "CELERY_BROKER_URL": "", "REDIS_URL": ""})
        cls.stack.enter_context(patch.dict(os.environ, selected, clear=True))
        cls.stack.enter_context(patch.object(tempfile, "tempdir", str(cls.root)))
        cls.stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        cls.guards = [cls.stack.enter_context(patch(target, side_effect=AssertionError("D58 external request forbidden")))
                      for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
                                     "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                                     "socket.getnameinfo", "openai.OpenAI", "openai.AsyncOpenAI",
                                     "smtplib.SMTP", "smtplib.SMTP_SSL")]
        cls.addClassCleanup(lambda: [guard.assert_not_called() for guard in cls.guards])
        cls.addClassCleanup(cls.assert_original_unchanged)
        cls.reference = inspect_source(cls.path)
        # Import only the local conversion domain, never app.main or a startup.
        from app.domain.pdf_conversion import convert_text_pdf
        cls.output = cls.root / "converted.epub"
        cls.summary = convert_text_pdf(cls.path, cls.output, cancel_check=lambda: False)
        require(cls.output.is_file(), "Real conversion returned without an EPUB")
        cls.snapshot = EpubSnapshot(cls.output)
        cls.output_sha256 = file_digest(cls.output)

    @classmethod
    def assert_original_unchanged(cls):
        require(file_identity(cls.path) == cls.original and file_digest(cls.path) == SOURCE_SHA256,
                "Historical source was modified")

    def tearDown(self):
        self.assert_original_unchanged()
        self.assertEqual(file_digest(self.output), self.output_sha256)

    def test_all_346_pages_and_all_167334_characters_survive_in_original_order(self):
        expected_pages = set(range(1, PAGE_COUNT + 1))
        self.assertEqual(set(self.snapshot.pages), expected_pages)
        self.assertEqual(normalized("".join(self.snapshot.unmapped)), "")
        total = 0
        for number, original in enumerate(self.reference["texts"], 1):
            actual = "".join(self.snapshot.pages[number])
            expected, actual = normalized(original), normalized(actual)
            with self.subTest(page=number):
                self.assertEqual(len(actual), len(expected))
                self.assertEqual(digest(actual.encode()), digest(expected.encode()), "Page text/order mismatch")
            total += len(actual)
        self.assertEqual(total, NORMALIZED_CHARACTERS)
        self.assertEqual(tuple(n for n in sorted(expected_pages) if not normalized("".join(self.snapshot.pages[n]))), IMAGE_ONLY_PAGES)
        for number in IMAGE_ONLY_PAGES:
            self.assertTrue(any(image["page"] == number for image in self.snapshot.images))

    def test_default_conversion_preserves_all_four_unapproved_for_removal_overlays(self):
        ad = normalized(ADVERTISEMENT)
        for number in AD_PAGES:
            text = normalized("".join(self.snapshot.pages[number]))
            self.assertEqual(text.count(ad), 1, "Default converter removed or duplicated historical overlay")
            self.assertTrue(text.endswith(ad), "Late overlay must remain separate from the original prose flow")

    def test_source_page_anchors_stay_with_the_first_actual_text_or_image(self):
        self.assertEqual(self.snapshot.marker_order, sorted(self.snapshot.marker_order))
        self.assertEqual(set(self.snapshot.pagebreaks), set(range(1, PAGE_COUNT + 1)))
        for number in range(1, PAGE_COUNT + 1):
            with self.subTest(page=number):
                anchors = self.snapshot.pagebreaks[number]
                self.assertEqual(len(anchors), 1)
                self.assertTrue(anchors[0][1])
                self.assertEqual(anchors[0][0], self.snapshot.first_content[number])

    def test_all_image_draws_objects_jpeg_bytes_rgb_and_42_alpha_planes_match_source(self):
        from PIL import Image
        actual_draws = Counter((image["page"], image["object"]) for image in self.snapshot.images)
        self.assertEqual(actual_draws, self.reference["draw_refs"])
        self.assertEqual(sum(actual_draws.values()), IMAGE_DRAWS)
        object_paths, path_objects = defaultdict(set), defaultdict(set)
        for image in self.snapshot.images:
            object_paths[image["object"]].add(image["path"])
            path_objects[image["path"]].add(image["object"])
        self.assertEqual(set(object_paths), set(self.reference["resources"]))
        self.assertEqual(len(path_objects), IMAGE_OBJECTS)
        self.assertEqual(set(path_objects), self.snapshot.manifest_images)
        self.assertTrue(all(len(paths) == 1 for paths in object_paths.values()))
        self.assertTrue(all(len(objects) == 1 for objects in path_objects.values()))
        masked = 0
        for identity, reference in self.reference["resources"].items():
            with self.subTest(object_id=identity):
                asset = self.snapshot.resource_bytes[next(iter(object_paths[identity]))]
                with Image.open(BytesIO(asset)) as image:
                    image.load()
                    self.assertEqual(image.size, reference["size"])
                    self.assertEqual(digest(image.convert("RGB").tobytes()), reference["rgb_sha256"])
                    if reference["alpha_sha256"] is not None:
                        masked += 1
                        self.assertEqual(image.format, "PNG")
                        self.assertEqual(image.mode, "RGBA")
                        self.assertEqual(digest(image.getchannel("A").tobytes()), reference["alpha_sha256"])
                    else:
                        self.assertEqual(image.format, "JPEG")
                        self.assertEqual(digest(asset), reference["encoded_sha256"])
        self.assertEqual(masked, SOFT_MASKS)

    def test_each_image_css_preserves_source_draw_ratio_not_only_native_pixels(self):
        expected = defaultdict(list)
        non_native_ratios = 0
        for draw in self.reference["draw_boxes"]:
            x0, top, x1, bottom = draw["bbox"]
            ratio = (x1 - x0) / (bottom - top)
            self.assertTrue(math.isfinite(ratio) and ratio > 0)
            expected[draw["page"], draw["object"]].append(ratio)
            pixel_width, pixel_height = self.reference["resources"][draw["object"]]["size"]
            if abs(ratio / (pixel_width / pixel_height) - 1) > .01:
                non_native_ratios += 1
        # The source itself stretches these drawings; retaining only native
        # JPEG/PNG aspect ratio would visibly change this book, especially p207.
        self.assertEqual(non_native_ratios, 20)
        actual = defaultdict(list)
        for image in self.snapshot.images:
            node = image["node"]
            parent = self.snapshot.parents[image["document"]].get(node)
            self.assertIsNotNone(parent)
            self.assertIn("image-box", parent.get("class", "").split())
            box_rule = linked_css_rule(self.snapshot, image["document"], ".image-box")
            self.assertEqual(box_rule.get("display"), "inline-block")
            self.assertEqual(box_rule.get("max-width"), "100%")
            image_rule = linked_css_rule(self.snapshot, image["document"], ".image-box img")
            effective = {**image_rule, **css_declarations(node.get("style", ""))}
            self.assertEqual(effective.get("width"), "100%")
            self.assertEqual(effective.get("height"), "auto")
            self.assertEqual(effective.get("object-fit"), "fill")
            dimensions = effective.get("aspect-ratio", "").split("/")
            self.assertEqual(len(dimensions), 2)
            width, height = map(float, dimensions)
            self.assertTrue(all(math.isfinite(value) and value > 0 for value in (width, height)))
            ratio = width / height
            actual[image["page"], image["object"]].append(ratio)
            box = css_declarations(parent.get("style", ""))
            bound = re.fullmatch(r"min\(([^,]+)em,\s*([^,]+)vh\)", box.get("width", ""))
            self.assertIsNotNone(bound, "Image width lacks a coupled viewport-height bound")
            em_width, vh_width = map(float, bound.groups())
            self.assertTrue(math.isfinite(em_width) and em_width > 0)
            self.assertAlmostEqual(vh_width / ratio, 65.0, places=4)
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(sum(map(len, actual.values())), IMAGE_DRAWS)
        for identity, expected_ratios in expected.items():
            with self.subTest(page=identity[0], object_id=identity[1]):
                observed = sorted(actual[identity])
                self.assertEqual(len(observed), len(expected_ratios))
                for found, wanted in zip(observed, sorted(expected_ratios)):
                    self.assertAlmostEqual(found / wanted, 1.0, places=7)

    def test_source_cover_is_identified_and_has_an_independent_spine_document(self):
        source_draws = [draw for draw in self.reference["draw_boxes"] if draw["page"] == 1]
        self.assertEqual(len(source_draws), 1)
        self.assertEqual(normalized(self.reference["texts"][0]), "")
        x0, y0, x1, y1 = source_draws[0]["bbox"]
        page_width, page_height = self.reference["page_boxes"][0]
        self.assertGreater((x1 - x0) * (y1 - y0) / (page_width * page_height), .5)
        self.assertTrue(any(item["page"] == 1 for item in self.reference["outlines"]))
        actual = [image for image in self.snapshot.images if image["page"] == 1]
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0]["object"], source_draws[0]["object"])
        self.assertEqual(self.snapshot.cover_images, {actual[0]["path"]})
        cover_document = actual[0]["document"]
        self.assertEqual(cover_document, self.snapshot.spine_documents[0])
        self.assertNotEqual(cover_document, self.snapshot.pagebreaks[2][0][0])
        body = self.snapshot.docs[cover_document].find("{*}body")
        self.assertIsNotNone(body)
        self.assertEqual({int(n.attrib[PAGE_ATTRIBUTE]) for n in body.iter() if PAGE_ATTRIBUTE in n.attrib}, {1})
        self.assertEqual(normalized("".join(body.itertext())), "")
        self.assertEqual([im["page"] for im in self.snapshot.images if im["document"] == cover_document], [1])

    def test_all_29_original_links_preserve_clicked_text_and_exact_internal_destinations(self):
        actual = defaultdict(list)
        for item in self.snapshot.internal_links:
            actual[item["node"].get("data-pdf-link-id")].append(item)
        expected = {item["id"]: item for item in self.reference["annotations"]}
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(len(actual), 29)
        for identifier, source in expected.items():
            with self.subTest(link=identifier):
                fragments = actual[identifier]
                self.assertTrue(fragments)
                text = ""
                destinations = set()
                for fragment in fragments:
                    node, document = fragment["node"], fragment["document"]
                    self.assertEqual(local_name(node.tag), "a")
                    self.assertEqual(node.get("data-pdf-link-source-page"), str(source["source_page"]))
                    self.assertEqual(node.get("data-pdf-link-target-page"), str(source["target_page"]))
                    page, prefix = mapped_source_prefix(self.snapshot, document, node)
                    self.assertEqual(page, source["source_page"])
                    self.assertEqual(digest(prefix.encode()), digest((source["source_prefix"] + text).encode()),
                                     "Clickable source range moved away from the original annotation rectangle")
                    text += "".join(node.itertext())
                    destinations.add(internal_target(document, node.get("href", "")))
                self.assertEqual(digest(text.encode()), digest(source["source_text"].encode()))
                self.assertEqual(len(destinations), 1)
                document, anchor_id = destinations.pop()
                self.assertTrue(anchor_id)
                target = self.snapshot.ids[document][anchor_id]
                if source["target_kind"] == "heading":
                    self.assertIn(local_name(target.tag), {"h1", "h2", "h3", "h4", "h5", "h6"})
                    self.assertEqual({int(n.attrib[PAGE_ATTRIBUTE]) for n in target.iter()
                                      if PAGE_ATTRIBUTE in n.attrib}, {source["target_page"]})
                    self.assertEqual(digest(normalized("".join(target.itertext())).encode()),
                                     digest(normalized(source["source_text"]).encode()))
                else:
                    self.assertEqual(local_name(target.tag), "span")
                    self.assertEqual("".join(target.itertext()), "")
                    self.assertNotEqual(target.get("class"), "pagebreak")
                    page, prefix = mapped_source_prefix(self.snapshot, document, target)
                    self.assertEqual(page, source["target_page"])
                    self.assertEqual(digest(prefix.encode()), digest(source["target_prefix"].encode()),
                                     "Footnote destination fell back to a page boundary or wrong glyph")
                    self.assertTrue(self.reference["texts"][page - 1][len(prefix):].startswith(source["target_text"]))

    def test_original_28_toc_titles_hierarchy_order_and_real_heading_targets_survive(self):
        selected = []
        for expected in self.reference["outlines"]:
            matches = [(index, item) for index, item in enumerate(self.snapshot.toc)
                       if normalized(item["title"]) == normalized(expected["title"])]
            self.assertEqual(len(matches), 1, "Original TOC title lost or duplicated")
            index, actual = matches[0]
            selected.append(index)
            self.assertEqual(actual["page"], expected["page"])
            self.assertEqual(actual["depth"], expected["depth"])
            if expected["title"] != "封面":
                self.assertIn(actual["target_tag"], {"h1", "h2", "h3", "h4", "h5", "h6"})
                self.assertEqual(digest(normalized(actual["target_text"]).encode()), digest(normalized(expected["title"]).encode()))
        self.assertEqual(selected, sorted(selected))

    def test_each_of_27_printed_toc_entries_has_its_own_complete_text_block(self):
        # Link count/targets alone do not establish readable printed TOC layout:
        # two correct anchors can still be merged into one prose paragraph.
        # Derive the entries from original PDF annotations and inspect their
        # actual nearest semantic block, never a converter-supplied entry label.
        source_entries = [item for item in self.reference["annotations"]
                          if item["source_page"] == 2 and item["target_kind"] == "heading"]
        self.assertEqual(len(source_entries), 27)
        links = defaultdict(list)
        for item in self.snapshot.internal_links:
            links[item["node"].get("data-pdf-link-id")].append(item)
        used_blocks = set()
        block_tags = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li"}
        for source in source_entries:
            with self.subTest(link=source["id"]):
                fragments = links[source["id"]]
                self.assertTrue(fragments)
                blocks = []
                for fragment in fragments:
                    document, node = fragment["document"], fragment["node"]
                    block = self.snapshot.parents[document].get(node)
                    while block is not None and local_name(block.tag) not in block_tags:
                        block = self.snapshot.parents[document].get(block)
                    self.assertIsNotNone(block, "Printed TOC entry has no semantic text block")
                    blocks.append((document, block))
                self.assertEqual(len(set(blocks)), 1, "One printed TOC entry was split across text blocks")
                identity = blocks[0]
                self.assertNotIn(identity, used_blocks, "Distinct printed TOC entries share one text block")
                used_blocks.add(identity)
                _, block = identity
                self.assertEqual({n.get("data-pdf-link-id") for n in block.iter()
                                  if "data-pdf-link-id" in n.attrib}, {source["id"]},
                                 "Another printed TOC entry was merged into this block")
                self.assertEqual({int(n.attrib[PAGE_ATTRIBUTE]) for n in block.iter()
                                  if PAGE_ATTRIBUTE in n.attrib and normalized("".join(n.itertext()))}, {2})
                self.assertEqual(digest(normalized("".join(block.itertext())).encode()),
                                 digest(normalized(source["source_text"]).encode()),
                                 "Printed TOC block contains unrelated text or loses part of its title")
        self.assertEqual(len(used_blocks), 27)

    def test_wrapped_heading_definition_inline_formula_and_cross_page_word_are_not_split(self):
        heading = "三、充足而又必要的条件（sufficientandnecessarycondition）"
        matches = [p for p in self.snapshot.text_blocks if normalized(p["text"]) == heading]
        self.assertEqual(len(matches), 1, "Page 75 wrapped subheading is not one text block")
        self.assertTrue(all(page == 75 for page, _ in matches[0]["spans"]))
        definitions = [p for p in self.snapshot.paragraphs if "definiendum" in p["text"] and "definiens" in p["text"]
                       and any(page == 273 for page, _ in p["spans"])]
        self.assertEqual(len(definitions), 1, "Page 273 definition has been fragmented")
        inline = [image for image in self.snapshot.images if image["object"] == (728, 0)]
        self.assertEqual(len(inline), 1)
        self.assertEqual(inline[0]["page"], 273)
        parent = inline[0]["node"]
        while parent is not None and local_name(parent.tag) != "p":
            parent = self.snapshot.parents[inline[0]["document"]].get(parent)
        self.assertIsNotNone(parent, "Page 273 inline relation symbol detached from its explanation")
        self.assertIn("表示填入", normalized("".join(parent.itertext())))
        joined = []
        for paragraph in self.snapshot.paragraphs:
            spans = [(page, normalized(text)) for page, text in paragraph["spans"] if normalized(text)]
            for (first_page, first), (second_page, second) in zip(spans, spans[1:]):
                if first_page == 294 and second_page == 295 and first.endswith("意") and second.startswith("思："):
                    joined.append(paragraph)
        self.assertEqual(len(joined), 1, "Cross-page Chinese word acquired a paragraph boundary")

    def test_lowered_formula_digits_keep_semantics_and_latin_wraps_keep_word_spaces(self):
        subs = defaultdict(list)
        for document, tree in self.snapshot.docs.items():
            for node in tree.iter():
                if local_name(node.tag) != "sub":
                    continue
                parent = self.snapshot.parents[document].get(node)
                while parent is not None and PAGE_ATTRIBUTE not in parent.attrib:
                    parent = self.snapshot.parents[document].get(parent)
                if parent is not None:
                    subs[int(parent.attrib[PAGE_ATTRIBUTE])].append("".join(node.itertext()))
        self.assertIn("2", subs[75], "Chemical formula subscript was flattened or moved")
        self.assertIn("1", subs[295], "Logical symbol subscript was flattened or moved")
        heading = next(p for p in self.snapshot.text_blocks
                       if normalized(p["text"]) == "三、充足而又必要的条件（sufficientandnecessarycondition）")
        self.assertIn("necessary condition", re.sub(r"\s+", " ", heading["text"]),
                      "Latin line wrap has only a visual gap, not a real word separator")

    def test_actual_epubcheck_has_no_errors_or_warnings(self):
        root = Path(__file__).resolve().parent.parent
        configured = os.environ.get("EPUBCHECK_JAR", "").strip()
        jar = Path(configured) if configured else root / "tools/epubcheck-5.1.0/epubcheck.jar"
        require(jar.is_file(), "Real EPUBCheck JAR required; no synthetic validation fallback")
        report = self.root / "epubcheck.json"
        command = ["java", "-Dhttp.proxyHost=127.0.0.1", "-Dhttp.proxyPort=1", "-Dhttps.proxyHost=127.0.0.1",
                   "-Dhttps.proxyPort=1", "-jar", str(jar), str(self.output), "--json", str(report)]
        reply = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=90,
                               env={key: value for key, value in os.environ.items() if key in {"PATH", "JAVA_HOME", "HOME", "TMPDIR"}})
        self.assertEqual(reply.returncode, 0, "Real EPUBCheck failed; no source content logged")
        require(report.is_file(), "EPUBCheck did not write its report")
        result = json.loads(report.read_text())
        messages = result.get("messages")
        self.assertIsInstance(messages, list)
        self.assertEqual(Counter(str(item.get("severity", "")).lower() for item in messages), Counter())

    def test_summary_original_and_external_call_boundaries(self):
        self.assertIsInstance(self.summary, dict)
        encoded = json.dumps(self.summary, ensure_ascii=False)
        self.assertNotIn(str(self.path), encoded)
        self.assertNotIn(ADVERTISEMENT, encoded)
        self.assertNotIn("encoded_stream_base64", encoded)
        self.assertNotIn("raw_text", encoded)
        self.assert_original_unchanged()
        for guard in self.guards:
            guard.assert_not_called()


def main():
    configured = os.environ.get(SOURCE_ENV, "").strip()
    if not configured or not Path(configured).is_file():
        print("Real PDF conversion gate requires the explicit EPUB_PDF_HISTORY_FILE sample.", file=sys.stderr)
        return 2
    program = unittest.main(verbosity=2, exit=False)
    result = program.result
    if not result.wasSuccessful() or not result.testsRun or result.skipped:
        return 1
    print(json.dumps({"verification": "one_real_chinese_pdf_content_and_structure_not_general_pdf_or_translation_quality",
                      "source_sha256": SOURCE_SHA256, "source_unchanged": True,
                      "epub_sha256": PdfConversionHistoryTests.output_sha256, "pages": PAGE_COUNT,
                      "normalized_characters": NORMALIZED_CHARACTERS, "default_advertising_overlays_retained": 4,
                      "image_draws": IMAGE_DRAWS, "image_objects": IMAGE_OBJECTS, "soft_masks": SOFT_MASKS,
                      "original_toc_entries": 28, "network_calls": sum(g.call_count for g in PdfConversionHistoryTests.guards),
                      "tests_run": result.testsRun, "skipped": len(result.skipped)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
