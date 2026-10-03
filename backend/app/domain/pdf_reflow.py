"""Bounded, local text-PDF reflow core. This is not OCR or a billing decision.

The caller owns process isolation, cancellation, EPUBCheck and publication.
Nothing here deletes advertisements, corrects words or performs CJK conversion.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import math
from pathlib import Path
import re
import statistics
import unicodedata
import zipfile

from lxml import etree as E
import pdfplumber
from pdfminer.utils import apply_matrix_pt, mult_matrix
from pypdf import PdfReader
from pypdf.generic import IndirectObject, NullObject

from .pdf_image_assets import export_images, PdfImageError

_X = "http://www.w3.org/1999/xhtml"
_EPUB = "http://www.idpf.org/2007/ops"
_OPF = "http://www.idpf.org/2007/opf"
_DC = "http://purl.org/dc/elements/1.1/"
_MESSAGES = {
    "invalid_input": "无法读取此 PDF。",
    "encrypted_pdf": "此 PDF 需要密码，暂不支持。",
    "extraction_forbidden": "此 PDF 不允许提取文字。",
    "input_limit": "PDF 超过当前转换资源限制。",
    "text_missing": "PDF 没有可用于重排的文字层。",
    "invalid_text": "PDF 文字层包含无法安全呈现的字符。",
    "layout_unsupported": "PDF 版式不能可靠重排，未生成成品。",
    "outline_unresolved": "PDF 目录无法可靠映射到正文。",
    "image_unsupported": "PDF 图像无法完整保留。",
    "invalid_output": "PDF 转换输出未通过完整性检查。",
}


class PdfReflowError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason if reason in _MESSAGES else "invalid_input"
        super().__init__(_MESSAGES[self.reason])


def _fail(reason):
    raise PdfReflowError(reason)


def _norm(value):
    return "".join(c for c in value if not c.isspace() and c != "\u200b")


def _sha(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _text_color(char):
    space, value = char.get("ncs"), char.get("non_stroking_color")
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    if space == "DeviceGray" and len(values) == 1:
        values *= 3
    elif space != "DeviceRGB" or len(values) != 3:
        _fail("layout_unsupported")
    if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
        _fail("layout_unsupported")
    return "rgb(" + ",".join(f"{v * 100:.8g}%" for v in values) + ")" if any(values) else None


def _text_ok(value):
    return isinstance(value, str) and all(
        c in "\t\n\r\u200b" or (c != "\ufffd" and unicodedata.category(c) not in {"Cc", "Cf", "Co", "Cs"}
        and (0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD
        or 0x10000 <= ord(c) <= 0x10FFFF)) for c in value)


def _child(parent, name, text=None, **attrs):
    node = E.SubElement(parent, f"{{{_X}}}{name}", {k.replace("_", "-"): str(v) for k, v in attrs.items()})
    node.text = text
    return node


def _append(node, text):
    if len(node):
        node[-1].tail = (node[-1].tail or "") + text
    else:
        node.text = (node.text or "") + text


def _document(title, language="und"):
    html = E.Element(f"{{{_X}}}html", nsmap={None: _X, "epub": _EPUB})
    html.set("lang", language)
    html.set("{http://www.w3.org/XML/1998/namespace}lang", language)
    head = _child(html, "head")
    _child(head, "title", title)
    _child(head, "meta", charset="utf-8")
    _child(head, "link", rel="stylesheet", type="text/css", href="styles.css")
    return html, _child(_child(html, "body"), "main")


def _xml(node):
    return E.tostring(node, encoding="utf-8", xml_declaration=True)


def _open_reader(source, max_pages):
    try:
        reader = PdfReader(source, strict=True)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                _fail("encrypted_pdf")
            if not unlocked:
                _fail("encrypted_pdf")
            permission = reader.trailer["/Encrypt"].get("/P")
            if (isinstance(permission, bool) or not isinstance(permission, int)
                    or not -(1 << 31) <= permission < (1 << 32)
                    or not (permission & 16)):
                _fail("extraction_forbidden")
        if "/OCProperties" in reader.trailer["/Root"]:
            _fail("layout_unsupported")
        count = len(reader.pages)
        if not 0 < count <= max_pages:
            _fail("input_limit")
        return reader
    except PdfReflowError:
        raise
    except Exception:
        _fail("invalid_input")


def _outline(reader):
    entries = []

    def walk(items, depth=0):
        if depth > 8 or len(entries) > 2000:
            _fail("input_limit")
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            try:
                title = item.title
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                _fail("outline_unresolved")
            if not _text_ok(title) or not _norm(title) or len(title) > 2000 or not 1 <= page <= len(reader.pages):
                _fail("outline_unresolved")
            xyz = None
            if item.get("/Type") == "/XYZ":
                values = [item.get("/Left"), item.get("/Top")]
                if all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(float(v)) for v in values):
                    xyz = tuple(map(float, values))
            entries.append({"title": title, "page": page, "depth": depth, "source_xyz": xyz})

    try:
        walk(reader.outline)
    except PdfReflowError:
        raise
    except Exception:
        _fail("outline_unresolved")
    if any(b["page"] < a["page"] or b["depth"] > a["depth"] + 1 for a, b in zip(entries, entries[1:])):
        _fail("outline_unresolved")
    return entries


def _bind_links(reader, pages, outline):
    """Map only explicit internal XYZ links; never guess an external action.

    Printed TOC labels must uniquely agree with the original outline. Other
    destinations must identify a single actual glyph; a page-top fallback would
    destroy a footnote/backlink, so an ambiguous target fails closed instead.
    """
    references = {(p.indirect_reference.idnum, p.indirect_reference.generation): i
                  for i, p in enumerate(reader.pages)}
    glyphs = [[c for row in p["lines"] for c in row["chars"]] for p in pages]
    records = []
    try:
        for index, pdf_page in enumerate(reader.pages):
            annotations = pdf_page.get("/Annots", [])
            if len(annotations) + len(records) > 10_000:
                _fail("input_limit")
            for reference in annotations:
                annotation = reference.get_object()
                if (annotation.get("/Subtype") != "/Link"
                        or any(key in annotation for key in ("/A", "/AA", "/AP", "/QuadPoints", "/OC"))):
                    _fail("layout_unsupported")
                flags = annotation.get("/F", 0)
                if isinstance(flags, bool) or not isinstance(flags, int) or flags & (1 | 2 | 32):
                    _fail("layout_unsupported")
                dest, rect = annotation.get("/Dest"), annotation.get("/Rect")
                if (not isinstance(dest, list) or len(dest) != 5 or dest[1] != "/XYZ"
                        or not isinstance(dest[0], IndirectObject)
                        or (dest[0].idnum, dest[0].generation) not in references
                        or not isinstance(rect, list) or len(rect) != 4
                        or not (isinstance(dest[4], NullObject) or dest[4] == 0)):
                    _fail("layout_unsupported")
                values = [*rect, dest[2], dest[3]]
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in values):
                    _fail("layout_unsupported")
                x0, y0, x1, y1 = map(float, rect)
                if x1 <= x0 or y1 <= y0:
                    _fail("layout_unsupported")
                left, _, _, top = map(float, pdf_page.mediabox)
                box = (x0 - left, top - y1, x1 - left, top - y0)
                source = glyphs[index]
                selected = [i for i, c in enumerate(source) if _norm(c["text"])
                            and box[0] <= (c["x0"] + c["x1"]) / 2 <= box[2]
                            and box[1] <= (c["top"] + c["bottom"]) / 2 <= box[3]]
                if not selected:
                    _fail("layout_unsupported")
                start, end = min(selected), max(selected)
                if [i for i in range(start, end + 1) if _norm(source[i]["text"])] != selected:
                    _fail("layout_unsupported")
                for c in source[start:end + 1]:
                    tolerance = c["size"] * .25
                    if c.get("link_id") or (_norm(c["text"]) and
                            (c["x0"] < box[0] - tolerance or c["x1"] > box[2] + tolerance
                             or c["top"] < box[1] - tolerance or c["bottom"] > box[3] + tolerance)):
                        _fail("layout_unsupported")
                target_index = references[dest[0].idnum, dest[0].generation]
                text = _norm("".join(c["text"] for c in source[start:end + 1]))
                targets = [item for item in outline if item["page"] == target_index + 1
                           and _norm(item["title"]) == text and item["source_xyz"] is not None
                           and all(abs(a - float(b)) < 1e-6 for a, b in zip(item["source_xyz"], dest[2:4]))]
                record = {"id": str(len(records) + 1), "source_page": index + 1, "target_page": target_index + 1}
                if len(targets) == 1:
                    record["outline"] = targets[0]
                elif targets:
                    _fail("layout_unsupported")
                else:
                    target_page = reader.pages[target_index]
                    target_left, _, _, target_top = map(float, target_page.mediabox)
                    x, y = float(dest[2]) - target_left, target_top - float(dest[3])
                    candidates = [(i, c) for i, c in enumerate(glyphs[target_index]) if _norm(c["text"])
                                  and abs(c["x0"] - x) <= c["size"] * .25
                                  and c["top"] - c["size"] * .25 <= y <= c["bottom"] + c["size"] * .25]
                    if len(candidates) != 1:
                        _fail("layout_unsupported")
                    position, char = candidates[0]
                    char["target_id"] = f"pdf-target-{target_index + 1}-{position}"
                    record["target_id"] = char["target_id"]
                records.append(record)
                for char in source[start:end + 1]:
                    char["link_id"] = record["id"]
    except PdfReflowError:
        raise
    except Exception:
        _fail("layout_unsupported")
    return records


def _lines(chars, page_number):
    """Keep content-stream character order; never sort letters by x coordinate."""
    lines, pending = [], []
    for c in chars:
        text = c.get("text", "")
        if not _text_ok(text) or re.fullmatch(r"\(cid:[0-9]+\)", text):
            _fail("invalid_text")
        c["render_color"] = _text_color(c)
        if not text.replace("\u200b", "").strip():
            if lines:
                lines[-1]["chars"].append(c)
            else:
                pending.append(c)
            continue
        if not c.get("upright", True):
            _fail("layout_unsupported")
        values = [c.get(k) for k in ("x0", "x1", "top", "bottom", "size")]
        if any(not isinstance(x, (float, int)) or not math.isfinite(x) for x in values) or c["size"] <= 0:
            _fail("layout_unsupported")
        cy = (c["top"] + c["bottom"]) / 2
        if lines and abs(cy - lines[-1]["cy"]) <= max(c["size"], lines[-1]["size"]) * .55:
            row = lines[-1]
            row["chars"].append(c)
            if c["size"] > row["size"] + .1:
                row["cy"], row["size"] = cy, c["size"]
        else:
            lines.append({"kind": "line", "page": page_number, "chars": pending + [c],
                          "cy": cy, "size": c["size"]})
            pending = []
    if pending:
        # Pages consisting only of whitespace still retain their source bytes.
        lines.append({"kind": "line", "page": page_number, "chars": pending,
                      "cy": 0.0, "size": 1.0})
    for row in lines:
        visible = [c for c in row["chars"] if _norm(c["text"])]
        row["text"] = "".join(c["text"] for c in row["chars"])
        row["fonts"] = sorted({c.get("fontname", "") for c in visible})
        for name, operation in (("x0", min), ("x1", max), ("top", min), ("bottom", max)):
            row[name] = operation(c[name] for c in visible) if visible else 0.0
        previous = None
        for position, b in enumerate(row["chars"]):
            if not _norm(b["text"]):
                continue
            # Large horizontal gaps can be tables or two simultaneous columns.
            # Explicit word spaces may be stretched by full justification.
            # Math combining marks may overlap: do not reorder them.
            if (previous is not None and b["x0"] - row["chars"][previous]["x1"] > row["size"] * 5
                    and not any(c["text"].isspace() for c in row["chars"][previous + 1:position])):
                _fail("layout_unsupported")
            previous = position
    if "".join(r["text"] for r in lines) != "".join(c["text"] for c in chars):
        _fail("invalid_text")
    return lines


def _separate_overlays(lines, body_size):
    """Small late text overlays are retained separately, not spliced into prose."""
    runs = [[]]
    for row in lines:
        if runs[-1] and row["cy"] < runs[-1][-1]["cy"] - max(body_size, row["size"]):
            runs.append([])
        runs[-1].append(row)
    if len(runs) == 1:
        return lines, []
    body = runs[0]
    extra = [r for run in runs[1:] for r in run]
    visible = sum(len(_norm(r["text"])) for r in body)
    overlay_chars = sum(len(_norm(r["text"])) for r in extra)
    if (len(runs) > 4 or overlay_chars > min(200, max(40, visible * .2))
            or any(r["size"] > body_size * 1.02 for r in extra)):
        _fail("layout_unsupported")
    for row in extra:
        row["overlay"] = True
    return body, extra


def _check_clips(page, pdf_page):
    """Only non-cutting rectangular viewport clips are supported.

    This deliberately checks *all* page marks against every clip, even marks
    outside that clip's graphics-state lifetime. It may reject a valid complex
    layout, but cannot quietly reintroduce content hidden by a supported clip.
    """
    content = pdf_page.get_contents()
    if content is None:
        return
    matrix, stack, path = (1, 0, 0, 1, 0, 0), [], []
    left, bottom, right, top = map(float, pdf_page.mediabox)
    if list(pdf_page.cropbox) != list(pdf_page.mediabox):
        _fail("layout_unsupported")
    marks = [c for c in page.chars if _norm(c.get("text", ""))]
    marks += page.images + page.lines + page.rects + page.curves
    for values, operator in content.operations:
        if operator == b"q":
            stack.append(matrix)
        elif operator == b"Q":
            if not stack:
                _fail("layout_unsupported")
            matrix = stack.pop()
        elif operator == b"cm":
            if len(values) != 6 or any(not math.isfinite(float(v)) for v in values):
                _fail("layout_unsupported")
            matrix = mult_matrix(tuple(map(float, values)), matrix)
        elif operator == b"Tr" and (len(values) != 1 or values[0] != 0):
            # Invisible/clip-text modes cannot be represented as plain text.
            _fail("layout_unsupported")
        elif operator in (b"BDC", b"BMC") and values and values[0] == "/OC":
            _fail("layout_unsupported")
        elif operator == b"Do":
            # The current exporter preserves image pixels, but does not perform
            # rotations, mirrors or shear. Positive anisotropic scale is kept in
            # CSS using the actual PDF draw ratio, not the intrinsic pixel ratio.
            a, b, c, d, _, _ = matrix
            if not all(math.isfinite(v) for v in matrix) or a <= 0 or d <= 0 or abs(b) > 1e-8 or abs(c) > 1e-8:
                _fail("layout_unsupported")
        elif operator in (b"m", b"l"):
            if operator == b"m" and path:
                path.append(None)  # Multiple subpaths are not one rectangle.
            path.append(apply_matrix_pt(matrix, tuple(map(float, values))))
        elif operator == b"re":
            x, y, w, h = map(float, values)
            if path:
                path.append(None)
            path.extend(apply_matrix_pt(matrix, point) for point in
                        ((x, y), (x + w, y), (x + w, y + h), (x, y + h)))
        elif operator in (b"c", b"v", b"y"):
            path.append(None)
        elif operator in (b"W", b"W*"):
            points = list(path)
            if points and points[0] == points[-1]:
                points.pop()
            if len(points) != 4 or None in points:
                _fail("layout_unsupported")
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            if (not all(math.isfinite(v) for v in (x0, x1, y0, y1)) or x1 <= x0 or y1 <= y0
                    or set(points) != {(x0, y0), (x0, y1), (x1, y0), (x1, y1)}
                    or any(a[0] != b[0] and a[1] != b[1] for a, b in zip(points, points[1:] + points[:1]))):
                _fail("layout_unsupported")
            clip = (x0 - left, top - y1, x1 - left, top - y0)
            if any(mark["x0"] < clip[0] - 1e-5 or mark["top"] < clip[1] - 1e-5
                   or mark["x1"] > clip[2] + 1e-5 or mark["bottom"] > clip[3] + 1e-5 for mark in marks):
                _fail("layout_unsupported")
        elif operator in (b"n", b"S", b"s", b"f", b"f*", b"B", b"B*", b"b", b"b*"):
            path = []
    if stack:
        _fail("layout_unsupported")


def _geometry(page, pdf_page):
    if page.rotation or pdf_page.get("/Rotate", 0) % 360:
        _fail("layout_unsupported")
    try:
        _check_clips(page, pdf_page)
    except PdfReflowError:
        raise
    except Exception:
        _fail("layout_unsupported")
    separators = []
    bars, caps = [], []
    for rectangle in page.rects:
        if not (0 < rectangle["height"] <= 4 and rectangle["width"] > 20 * rectangle["height"]):
            caps.append(rectangle)
            continue
        match = next((bar for bar in bars if abs(bar["x0"] - rectangle["x0"]) < .5
                      and abs(bar["x1"] - rectangle["x1"]) < .5
                      and max(bar["bottom"], rectangle["bottom"]) - min(bar["top"], rectangle["top"]) <= 4), None)
        if match:
            match["top"] = min(match["top"], rectangle["top"])
            match["bottom"] = max(match["bottom"], rectangle["bottom"])
        else:
            bars.append({k: rectangle[k] for k in ("x0", "x1", "top", "bottom")})
    for curve in page.curves + caps:
        # A thin horizontal border can have tiny polygon end caps. No Bezier
        # curve, disconnected shape, diagram or arbitrary vertical path passes.
        if any(op[0] not in {"m", "l", "h"} for op in curve.get("path", [])):
            _fail("layout_unsupported")
        if not any(curve["width"] <= 4 and curve["height"] <= 4
                   and curve["top"] >= bar["top"] - .1 and curve["bottom"] <= bar["bottom"] + .1
                   and (abs(curve["x0"] - bar["x0"]) <= .1 or abs(curve["x1"] - bar["x1"]) <= .1)
                   for bar in bars):
            _fail("layout_unsupported")
    separators.extend({"kind": "separator", "top": bar["top"], "x0": bar["x0"], "x1": bar["x1"]} for bar in bars)
    for line in page.lines:
        if (abs(line["y1"] - line["y0"]) > .5 or line["x1"] <= line["x0"]
                or line.get("linewidth", 1) > 4):
            _fail("layout_unsupported")
        separators.append({"kind": "line-decoration", "top": line["top"], "x0": line["x0"], "x1": line["x1"]})
    return separators


def _add_line(parent, row, word_gap=False):
    if word_gap:
        _child(parent, "span", " ", data_generated_spacing="true")
    span = _child(parent, "span", data_pdf_page=row["page"])
    normal = [c for c in row["chars"] if c["size"] >= row["size"] * .9 and _norm(c["text"])]
    bottom = statistics.median(c["bottom"] for c in normal) if normal else row["bottom"]
    link = None
    link_id = None
    for c in row["chars"]:
        text = c["text"]
        small = bool(_norm(text)) and c["size"] < row["size"] * .85
        if c.get("link_id") != link_id:
            link_id = c.get("link_id")
            link = _child(span, "a", href="#", data_pdf_link_id=link_id) if link_id else None
        container = link if link is not None else span
        if c.get("target_id"):
            _child(container, "span", id=c["target_id"])
        if c.get("render_color"):
            container = _child(container, "span", style="color:" + c["render_color"])
        if any(c["x0"] >= x["x0"] - .5 and c["x1"] <= x["x1"] + .5 for x in row.get("underlines", [])):
            container = _child(container, "u")
        if small and c["bottom"] - bottom > row["size"] * .08:
            _child(container, "sub", text)
        elif small and bottom - c["bottom"] > row["size"] * .2:
            _child(container, "sup", text)
        else:
            _append(container, text)
    return span


_CSS = """@charset "UTF-8";
body{font-family:serif;line-height:1.85;margin:1em 6%;overflow-wrap:break-word}
main{max-width:36em;margin:auto}p{margin:0 0 .65em;text-align:justify;text-indent:2em}
p.noindent,p.formula,p.overlay{text-indent:0}p.formula{margin-left:2em;text-align:left}
p.overlay{font-size:.85em;border-top:1px dotted #aaa;margin-top:1em}
.cover-page{break-after:page;page-break-after:always}.cover-page figure{margin:0}
h1,h2,h3{text-indent:0;line-height:1.6;break-after:avoid;page-break-after:avoid}
h1,h2{text-align:center}h3{font-size:1.1em;margin:1.2em 0 .65em}
p.center{text-align:center;text-indent:0}p.right{text-align:right;text-indent:0}
figure{text-align:center;margin:1em 0;break-inside:avoid;page-break-inside:avoid}
.image-box{display:inline-block;max-width:100%;vertical-align:middle}
.image-box img{display:block;width:100%;height:auto;object-fit:fill}
.inline-image{vertical-align:baseline}
sub,sup{font-size:.75em;line-height:0}hr{margin:1em 0}
.pagebreak{display:inline}nav li{margin:.4em 0}a{color:inherit}
"""


def build_text_pdf(source: Path, output: Path, *, max_pages=500,
                   max_page_chars=1_000_000, max_total_chars=10_000_000) -> dict:
    """Create one private EPUB. No OCR, automatic cleanup, billing or publication."""
    source, output = Path(source), Path(output)
    if any(type(value) is not int or not 0 < value <= maximum for value, maximum in
           ((max_pages, 500), (max_page_chars, 1_000_000), (max_total_chars, 10_000_000))):
        _fail("input_limit")
    if not source.is_file() or source.is_symlink() or output.exists() or output.is_symlink():
        _fail("invalid_input")
    if source.stat().st_size > 50 * 1024 * 1024:
        _fail("input_limit")
    digest = _sha(source.read_bytes())
    reader = _open_reader(source, max_pages)
    outline = _outline(reader)
    try:
        assets = export_images(reader)
    except PdfImageError:
        _fail("image_unsupported")
    warnings = set()
    if reader.is_encrypted:
        warnings.add("empty_password_encryption")
    pages, total_chars = [], 0
    font_counts = Counter()
    try:
        with pdfplumber.open(source, password="") as pdf:
            if len(pdf.pages) != len(reader.pages):
                _fail("invalid_input")
            for number, page in enumerate(pdf.pages, 1):
                chars = page.chars
                text = "".join(c.get("text", "") for c in chars)
                total_chars += len(text)
                if len(text) > max_page_chars or total_chars > max_total_chars:
                    _fail("input_limit")
                separators = _geometry(page, reader.pages[number - 1])
                lines = _lines(chars, number)
                genuine_separators = []
                for decoration in separators:
                    matches = [row for row in lines if decoration["kind"] == "line-decoration"
                               and row["top"] < decoration["top"] <= row["bottom"] + row["size"] * .35
                               and decoration["x0"] >= row["x0"] - 1 and decoration["x1"] <= row["x1"] + 1]
                    if len(matches) == 1:
                        matches[0].setdefault("underlines", []).append(decoration)
                    else:
                        # A short mathematical fraction bar is not a page-level
                        # divider. Only wide rules spanning the text area pass.
                        visible = [c for c in chars if _norm(c.get("text", ""))]
                        if (not visible or decoration["x1"] - decoration["x0"] < page.width * .5
                                or decoration["x0"] > min(c["x0"] for c in visible) + 1):
                            _fail("layout_unsupported")
                        genuine_separators.append(dict(decoration, kind="separator"))
                separators = genuine_separators
                for c in chars:
                    if _norm(c.get("text", "")):
                        font_counts[round(c["size"] * 4) / 4] += len(_norm(c["text"]))
                images = []
                for image in page.images:
                    keys = [k for k in assets if k[0] == image["stream"].objid]
                    if len(keys) != 1 or image["width"] <= 0 or image["height"] <= 0:
                        _fail("image_unsupported")
                    images.append({k: image[k] for k in ("x0", "x1", "top", "bottom", "width", "height")}
                                  | {"kind": "image", "key": keys[0], "page": number})
                if not _norm(text):
                    warnings.add("pages_without_extractable_text")
                pages.append({"number": number, "text": text, "lines": lines, "images": images,
                              "separators": separators, "width": page.width, "height": page.height})
                page.close()
    except PdfReflowError:
        raise
    except Exception:
        _fail("invalid_input")
    if not font_counts or not sum(len(_norm(p["text"])) for p in pages):
        _fail("text_missing")
    links = _bind_links(reader, pages, outline)
    outline_link_ids = {record["id"] for record in links if "outline" in record}
    for page in pages:
        for row in page["lines"]:
            # Only a complete source-linked label, already verified against its
            # outline title and XYZ destination, proves a printed TOC boundary.
            # Whitespace/zero-width glyphs do not change that identity, but one
            # visible unlinked glyph makes the row ordinary prose again.
            identities = {char.get("link_id") for char in row["chars"] if _norm(char["text"])}
            if len(identities) == 1 and next(iter(identities)) in outline_link_ids:
                row["toc_entry"] = next(iter(identities))
    body_size = font_counts.most_common(1)[0][0]
    all_body, deltas = [], []
    for page in pages:
        flow, overlays = _separate_overlays(page["lines"], body_size)
        page["flow"], page["overlays"] = flow, overlays
        if overlays:
            warnings.add("late_text_overlay_preserved")
        normal = [x for x in flow if abs(x["size"] - body_size) < body_size * .08 and _norm(x["text"])]
        all_body.extend(normal)
        deltas.extend(b["top"] - a["top"] for a, b in zip(normal, normal[1:])
                      if body_size * .8 <= b["top"] - a["top"] <= body_size * 2.5)
    if not all_body:
        _fail("layout_unsupported")
    leading = Counter(round(x * 2) / 2 for x in deltas).most_common(1)[0][0] if deltas else body_size * 1.6
    left = Counter(round(x["x0"] * 2) / 2 for x in all_body).most_common(1)[0][0]
    right = statistics.quantiles([x["x1"] for x in all_body], n=10)[8] if len(all_body) >= 10 else max(x["x1"] for x in all_body)
    if right - left < body_size * 8:
        _fail("layout_unsupported")

    # Bind each source bookmark to exactly one within-page text span. Pure-image
    # page bookmarks are valid page destinations; no invented title text is added.
    for page in pages:
        records = [x for x in outline if x["page"] == page["number"]]
        for item in records:
            if not _norm(page["text"]):
                item["page_only"] = True
                continue
            rows = page["flow"]
            candidates = []
            for index in range(len(rows)):
                value = ""
                for end in range(index, min(index + 5, len(rows))):
                    if rows[end]["kind"] != "line":
                        break
                    value += _norm(rows[end]["text"])
                    if value == _norm(item["title"]):
                        candidates.append((index, end))
                    if not _norm(item["title"]).startswith(value):
                        break
            if len(candidates) != 1:
                _fail("outline_unresolved")
            index, end = candidates[0]
            group = rows[index:end + 1]
            rows[index:end + 1] = [dict(group[0], kind="heading", lines=group, outline=item)]

    # A cover is inferred only from a source-backed first-page destination and
    # a single majority-area illustration. Small logos or text-bearing first
    # pages are not promoted. This changes packaging, never image bytes.
    first_page = pages[0]
    cover_key = None
    if (not _norm(first_page["text"]) and len(first_page["images"]) == 1
            and any(item.get("page_only") and item["page"] == first_page["number"] for item in outline)):
        image = first_page["images"][0]
        if (image["width"] * image["height"] > first_page["width"] * first_page["height"] * .5
                and image["x0"] >= 0 and image["top"] >= 0
                and image["x1"] <= first_page["width"] and image["bottom"] <= first_page["height"]):
            cover_key = image["key"]

    title = str((reader.metadata or {}).get("/Title") or source.stem)
    author = str((reader.metadata or {}).get("/Author") or "")
    if not _text_ok(title) or not _text_ok(author) or len(title) > 2000 or len(author) > 2000:
        _fail("invalid_text")
    language = reader.trailer["/Root"].get("/Lang", "und")
    if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language):
        language = "und"
    parts, page_targets = [], {}
    current = parent = paragraph = last = None
    fresh_anchor = None
    paragraph_count = 0

    def start_part(name):
        nonlocal current, parent, paragraph, last
        html, parent = _document(name, language)
        current = {"name": f"part-{len(parts):04d}.xhtml", "root": html}
        parts.append(current)
        paragraph = last = None

    def image_node(container, image, inline=False):
        asset = assets[image["key"]]
        box = _child(container, "span", **{"class": "image-box" + (" inline-image" if inline else "")},
                     style=f"width:min({image['width'] / body_size:.8g}em,{65 * image['width'] / image['height']:.8g}vh)")
        _child(box, "img", src="images/" + asset["filename"], alt=f"原 PDF 第 {image['page']} 页插图",
               width=asset["pixel_size"][0], height=asset["pixel_size"][1],
               style=f"aspect-ratio:{image['width']:.10g} / {image['height']:.10g};object-fit:fill",
               data_pdf_page_image=image["page"], data_pdf_object=f"{image['key'][0]}:{image['key'][1]}")

    start_part(title)
    if cover_key is not None:
        parent.set("class", "cover-page")
    heading_count = 0
    for page in pages:
        number = page["number"]
        if cover_key is not None and number == first_page["number"] + 1:
            # A separate spine document plus explicit break keeps the printed
            # TOC/body from sharing the original cover's reader page.
            start_part(title)
        fresh_anchor = _child(paragraph if paragraph is not None else parent, "span", id=f"page-{number}",
                              data_pdf_page=number, **{"class": "pagebreak", "role": "doc-pagebreak", "aria-label": number})
        fresh_anchor.set(f"{{{_EPUB}}}type", "pagebreak")
        page_targets[number] = current["name"] + f"#page-{number}"
        events = sorted(page["flow"] + page["images"] + page["separators"], key=lambda x: (x["top"], x["x0"]))
        # Flow was already verified to be monotonic. Images may interleave text,
        # but an image cannot cover text or silently replace a glyph.
        inline_for = {}
        for image in page["images"]:
            overlaps = [row for row in page["flow"] if row["kind"] == "line"
                        and row["top"] < image["bottom"] and row["bottom"] > image["top"]]
            if overlaps:
                if len(overlaps) != 1 or image["x1"] > overlaps[0]["x0"] + body_size * .1:
                    _fail("layout_unsupported")
                inline_for[id(overlaps[0])] = image
        inline_images = {id(x) for x in inline_for.values()}
        for event in events + page["overlays"]:
            kind = event["kind"]
            if kind == "image" and id(event) in inline_images:
                continue
            if kind == "heading":
                heading_count += 1
                item = event["outline"]
                start_part(item["title"])
                if fresh_anchor is not None:
                    fresh_anchor.getparent().remove(fresh_anchor)
                    parent.append(fresh_anchor)
                    page_targets[number] = current["name"] + f"#page-{number}"
                    fresh_anchor = None
                node = _child(parent, "h1" if item["depth"] == 0 else "h2", id=f"heading-{heading_count}")
                for line in event["lines"]:
                    _add_line(node, line)
                item["href"] = current["name"] + f"#heading-{heading_count}"
                continue
            if kind == "image":
                fresh_anchor = None
                paragraph = last = None
                image_node(_child(parent, "figure"), event)
                continue
            if kind == "separator":
                _child(parent, "hr", **{"class": "footnote-rule"})
                paragraph = last = None
                continue
            row = event
            fresh_anchor = None
            if not row["text"]:
                continue
            width = row["x1"] - row["x0"]
            center = width < (right - left) * .8 and abs((row["x0"] + row["x1"]) / 2 - page["width"] / 2) < body_size * .55 and row["x0"] > left + body_size * 2
            heading = row["size"] > body_size * 1.1
            formula = bool(re.match(r"^[（(]?[AEIOPQRXYZSＭＳＰＡＢＣＤ][.．、:： ）)]", row["text"]))
            css = "overlay" if row.get("overlay") else "subheading" if heading else "center" if center else "formula" if formula else ""
            same_page = last is not None and last["page"] == number
            continuation_heading = bool(last and paragraph is not None and paragraph.tag == f"{{{_X}}}h3" and heading
                and same_page and abs(row["size"] - last["size"]) < .1
                and abs(row["x0"] - last["x0"]) < body_size * .1
                and 0 < row["top"] - last["top"] <= leading * 1.15
                and last["x1"] > right - body_size
                and not re.match(r"^[一二三四五六七八九十0-9]+[、．.)）]", row["text"]))
            new = (paragraph is None or css != "" or paragraph.get("class", "") not in {"", "noindent"}
                   or row["x0"] > left + body_size
                   or (same_page and row["top"] - last["top"] > leading * 1.22)
                   or (not same_page and last is not None and last["text"].rstrip().endswith(tuple("。！？：；.!?:;"))))
            if continuation_heading:
                new = False
            if row.get("toc_entry"):
                # Source TOC entries can have equal fonts/spacing and dedent
                # from a child to a parent. Preserve their semantic boundary,
                # while keeping a single wrapped annotation in one block.
                new = not (paragraph is not None and same_page
                           and last.get("toc_entry") == row["toc_entry"])
            elif last and last.get("toc_entry"):
                new = True
            if id(row) in inline_for:
                new = True
            if new:
                paragraph = _child(parent, "h3" if heading and not row.get("overlay") else "p")
                paragraph_count += 1
                if css and css != "subheading":
                    paragraph.set("class", css)
                elif not heading and row["x0"] <= left + body_size * .5:
                    paragraph.set("class", "noindent")
                if row.get("toc_entry"):
                    paragraph.set("data-pdf-toc-entry", row["toc_entry"])
            if id(row) in inline_for:
                image_node(paragraph, inline_for[id(row)], inline=True)
            gap = bool(not new and last and re.search(r"[A-Za-z]$", last["text"]) and re.match(r"^[A-Za-z]", row["text"]))
            _add_line(paragraph, row, gap)
            last = row
    for item in outline:
        if item.get("page_only"):
            item["href"] = page_targets[item["page"]]
        if "href" not in item:
            _fail("outline_unresolved")

    target_paths = {node.get("id"): part["name"] + "#" + node.get("id")
                    for part in parts for node in part["root"].xpath("//*[@id]")}
    link_records = {record["id"]: record for record in links}
    for part in parts:
        for node in part["root"].xpath("//*[@data-pdf-link-id]"):
            record = link_records[node.get("data-pdf-link-id")]
            href = record["outline"].get("href") if "outline" in record else target_paths.get(record["target_id"])
            if not href:
                _fail("layout_unsupported")
            node.set("href", href)
            node.set("data-pdf-link-source-page", str(record["source_page"]))
            node.set("data-pdf-link-target-page", str(record["target_page"]))

    actual = {p["number"]: [] for p in pages}
    for part in parts:
        for node in part["root"].xpath("//*[@data-pdf-page]"):
            if node.get("class") != "pagebreak":
                actual[int(node.get("data-pdf-page"))].append("".join(node.itertext()))
    if any("".join(actual[p["number"]]) != p["text"] for p in pages):
        _fail("invalid_text")
    observed_images = Counter((int(node.get("data-pdf-page-image")), node.get("data-pdf-object"))
        for part in parts for node in part["root"].xpath("//*[@data-pdf-object]"))
    wanted_images = Counter((p["number"], f"{im['key'][0]}:{im['key'][1]}") for p in pages for im in p["images"])
    if observed_images != wanted_images:
        _fail("invalid_output")

    nav, navbody = _document(title + " · 导航", language)
    toc = _child(navbody, "nav", id="toc")
    toc.set(f"{{{_EPUB}}}type", "toc")
    _child(toc, "h1", "目录")
    ol = _child(toc, "ol")
    levels, last_li = [ol], None
    for item in outline or [{"title": title, "depth": 0, "href": page_targets[1]}]:
        depth = item["depth"]
        while len(levels) > depth + 1:
            levels.pop()
        while len(levels) < depth + 1:
            if last_li is None:
                _fail("outline_unresolved")
            levels.append(_child(last_li, "ol"))
        last_li = _child(levels[-1], "li")
        _child(last_li, "a", item["title"], href=item["href"])
    pn = _child(navbody, "nav", id="pages", hidden="hidden")
    pn.set(f"{{{_EPUB}}}type", "page-list")
    _child(pn, "h2", "原 PDF 页码")
    pl = _child(pn, "ol")
    for number, href in page_targets.items():
        _child(_child(pl, "li"), "a", str(number), href=href)
    package = E.Element(f"{{{_OPF}}}package", nsmap={None: _OPF}, version="3.0", attrib={"unique-identifier": "book-id"})
    meta = E.SubElement(package, f"{{{_OPF}}}metadata", nsmap={"dc": _DC})
    for name, value in (("identifier", "urn:sha256:" + digest + ":pdf-text-epub-v1"), ("title", title), ("language", language), ("creator", author)):
        item = E.SubElement(meta, f"{{{_DC}}}{name}")
        item.text = value
        if name == "identifier":
            item.set("id", "book-id")
    E.SubElement(meta, f"{{{_OPF}}}meta", property="dcterms:modified").text = "1980-01-01T00:00:00Z"
    manifest = E.SubElement(package, f"{{{_OPF}}}manifest")
    spine = E.SubElement(package, f"{{{_OPF}}}spine")
    def manifest_item(id_, href, mime, **attrs):
        E.SubElement(manifest, f"{{{_OPF}}}item", id=id_, href=href, attrib={"media-type": mime, **attrs})
    manifest_item("nav", "nav.xhtml", "application/xhtml+xml", properties="nav")
    manifest_item("css", "styles.css", "text/css")
    members = {"EPUB/nav.xhtml": _xml(nav), "EPUB/styles.css": _CSS.encode("utf-8")}
    for index, part in enumerate(parts):
        manifest_item(f"part-{index}", part["name"], "application/xhtml+xml")
        E.SubElement(spine, f"{{{_OPF}}}itemref", idref=f"part-{index}")
        members["EPUB/" + part["name"]] = _xml(part["root"])
    for index, (key, asset) in enumerate(assets.items()):
        filename = asset["filename"]
        if Path(filename).name != filename or filename in {"", ".", ".."}:
            _fail("image_unsupported")
        attributes = {"properties": "cover-image"} if key == cover_key else {}
        manifest_item(f"image-{index}", "images/" + filename, asset["mime"], **attributes)
        members["EPUB/images/" + filename] = asset["data"]
    members["EPUB/package.opf"] = _xml(package)
    members["META-INF/container.xml"] = b'<?xml version="1.0" encoding="UTF-8"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    created_output = False
    try:
        with output.open("xb") as stream:
            created_output = True
            with zipfile.ZipFile(stream, "w") as z:
                for name, data in [("mimetype", b"application/epub+zip"), *sorted(members.items())]:
                    entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    entry.external_attr = 0o600 << 16
                    z.writestr(entry, data, compress_type=zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED)
    except Exception:
        if created_output:
            output.unlink(missing_ok=True)
        _fail("invalid_output")
    if _sha(source.read_bytes()) != digest:
        output.unlink(missing_ok=True)
        _fail("invalid_input")
    return {"schema_version": "pdf-text-epub-v1", "source_sha256": digest, "output_sha256": _sha(output.read_bytes()),
            "page_count": len(pages), "normalized_characters": sum(len(_norm(p["text"])) for p in pages),
            "zero_width_spaces_preserved": sum(p["text"].count("\u200b") for p in pages),
            "image_assets": len(assets), "image_placements": sum(wanted_images.values()),
            "toc_entries": len(outline), "paragraph_count": paragraph_count, "warnings": sorted(warnings),
            "pages": [{"number": p["number"], "text_sha256": _sha(p["text"]), "normalized_sha256": _sha(_norm(p["text"])),
                       "char_count": len(p["text"]), "normalized_char_count": len(_norm(p["text"])),
                       "image_placements": len(p["images"])} for p in pages]}
