"""Bounded PDFium subprocesses: untrusted documents never run in API threads.

Page snapshots retain diagrams/formulae; they are explicitly original-language
references, not translated images or a layout-preserving PDF product.
"""
from contextlib import closing
import hashlib
import ctypes
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from .config import PdfError, positive_int
from .text_quality import assess
from .native_layout import extract_blocks


def unsafe_symbol_font(name):
    # Decorative marks can have plausible but wrong Unicode (e.g. an emphasis
    # dot encoded as 3). Never repair these by replacing the source character.
    return bool(re.search(r'^(?:CM(?:MI|SY|EX)|MS[AB]M)\d|math|symbol|kenten|circle.?digital|dingbat|wingding|webding',
                          name.split('+')[-1], re.I))


def _columns(rects):
    # PDF object order is not necessarily visual reading order. Group nearby rows
    # first so column-major and interleaved streams receive the same assessment.
    rows = []
    for rect in sorted(rects, key=lambda r: -(r[1] + r[3]) / 2):
        center = (rect[1] + rect[3]) / 2
        if not rows or abs(rows[-1][0] - center) > 8:
            rows.append((center, [rect]))
        else:
            rows[-1][1].append(rect)
    return sum(any(b[0] - a[2] > 35 for a, b in zip(parts, parts[1:]))
               for _, row in rows for parts in [sorted(row)]) >= 3


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def worker(operation, path, *args):
    try:
        process = subprocess.run(
            [sys.executable, '-m', 'app.domain.pdf.local', operation, str(path), *map(str, args)],
            cwd=Path(__file__).resolve().parents[3], capture_output=True, timeout=90,
            # Rendering does not need API keys or the application's .env.
            env={k: v for k, v in os.environ.items() if k in {'PATH', 'SYSTEMROOT', 'LANG', 'TMPDIR'}},
        )
        if process.returncode:
            raise PdfError("PDF 无法解析、受密码保护或超出处理限制，请检查原文件")
        return json.loads(process.stdout)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        raise PdfError("PDF 本地解析超时或失败，未发起 OCR 请求") from None


def inspect_pdf(path):
    result = worker('inspect', path, positive_int('PDF_MAX_PAGES', 1000, 3000))
    result['sha256'] = digest(path)
    return result


def render_page(path, page, destination):
    worker('render', path, page, destination)
    os.chmod(destination, 0o600)
    return Path(destination)


def _inspect(path, limit):
    import pypdfium2 as pdfium
    with closing(pdfium.PdfDocument(path)) as doc:
        if not 0 < len(doc) <= limit:
            raise ValueError('page limit')
        pages = []
        for index in range(len(doc)):
            with closing(doc[index]) as page:
                width, height = page.get_size()
                if min(width, height) <= 0 or max(width, height) > 14400:
                    raise ValueError('page size')
                with closing(page.get_textpage()) as tp:
                    if tp.count_chars() > 100000:
                        raise ValueError('page text limit')
                    text = tp.get_text_bounded().strip()
                    # Independent text rectangles separated horizontally suggest columns.
                    count = tp.count_rects()
                    rects = [tp.get_rect(i) for i in range(min(count, 2000))]
                multi_column = _columns(rects)
                image_area = 0
                object_count, paths, invisible = 0, 0, False
                # An annotation/widget may paint content absent from the text layer.
                annotations = pdfium.raw.FPDFPage_GetAnnotCount(page)
                uncertain_geometry = count > 2000 or annotations < 0
                for number in range(max(0, annotations)):
                    annotation = pdfium.raw.FPDFPage_GetAnnot(page, number)
                    try:
                        # A plain hyperlink adds no source text. Keep widgets,
                        # free-text and links with appearance streams conservative.
                        uncertain_geometry |= (not annotation or
                            pdfium.raw.FPDFAnnot_GetSubtype(annotation) != pdfium.raw.FPDF_ANNOT_LINK or
                            bool(pdfium.raw.FPDFAnnot_HasKey(annotation, b'AP')))
                    finally:
                        if annotation:
                            pdfium.raw.FPDFPage_CloseAnnot(annotation)
                math_layout = False
                for obj in page.get_objects():
                    object_count += 1
                    if object_count > 10000:
                        uncertain_geometry = True
                        break
                    # Nested form transforms and rotated text require layout analysis;
                    # do not treat object-local coordinates as page-global positions.
                    if obj.type == pdfium.raw.FPDF_PAGEOBJ_FORM:
                        uncertain_geometry = True
                    if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                        x0, y0, x1, y1 = obj.get_bounds()
                        image_area += max(0, min(width, x1) - max(0, x0)) * max(0, min(height, y1) - max(0, y0))
                    elif obj.type == pdfium.raw.FPDF_PAGEOBJ_PATH:
                        paths += 1
                    elif obj.type == pdfium.raw.FPDF_PAGEOBJ_TEXT:
                        font = pdfium.raw.FPDFTextObj_GetFont(obj)
                        name = ctypes.create_string_buffer(256)
                        if font:
                            pdfium.raw.FPDFFont_GetBaseFontName(font, name, len(name))
                            font_name = name.value.decode('utf8', errors='replace').split('+')[-1]
                            # Math fonts can map a drawn radical to ordinary 'p'.
                            # Readable Unicode alone is not proof of faithful extraction.
                            math_layout |= unsafe_symbol_font(font_name)
                        mode = pdfium.raw.FPDFTextObj_GetTextRenderMode(obj)
                        invisible |= mode in {pdfium.raw.FPDF_TEXTRENDERMODE_INVISIBLE, pdfium.raw.FPDF_TEXTRENDERMODE_CLIP, -1}
                        matrix = obj.get_matrix()
                        uncertain_geometry |= abs(matrix.b) > .01 or abs(matrix.c) > .01
                assessment = assess(text, image_ratio=image_area / (width * height),
                                    multi_column=multi_column, invisible=invisible,
                                    complex_vectors=paths >= 12 or (paths > 0 and len(text.strip()) < 40),
                                    math_layout=math_layout,
                                    uncertain_geometry=uncertain_geometry, has_objects=object_count > 0 or annotations != 0)
                blocks = []
                if assessment['route'] == 'local':
                    try:
                        with closing(page.get_textpage()) as layout_text:
                            blocks = extract_blocks(layout_text)
                    except (ValueError, IndexError, RuntimeError):
                        assessment['route'] = 'ocr'
                        assessment['reasons'].insert(0, 'native_layout_ambiguous')
                if assessment['route'] == 'local' and ''.join(text.split()) != ''.join(''.join(b['source_text'] for b in blocks).split()):
                    assessment['route'] = 'ocr'
                    assessment['reasons'].insert(0, 'native_layout_text_mismatch')
                # Sparse/vector/image pages are never assumed blank merely because text extraction is empty.
                label = doc.get_page_label(index) or str(index + 1)
                pages.append({'page': index + 1, 'label': label[:80], 'text': text,
                              'route': assessment['route'], 'assessment': assessment,
                              'reason': assessment['reasons'][0],
                              'width': width, 'height': height, 'blocks': blocks})
        scans = sum(p['route'] == 'ocr' for p in pages)
        kinds = {p['assessment']['source_kind'] for p in pages} - {'blank'}
        kind = 'text' if not kinds or kinds == {'text'} else 'scan' if kinds == {'scan'} else 'mixed'
        return {'version': 5, 'title': (doc.get_metadata_value('Title') or '')[:500], 'page_count': len(pages), 'ocr_pages': scans,
                'kind': kind, 'pages': pages}


def _render(path, index, target):
    import pypdfium2 as pdfium
    with closing(pdfium.PdfDocument(path)) as doc:
        if not 1 <= index <= len(doc):
            raise ValueError('page range')
        with closing(doc[index - 1]) as page:
            width, height = page.get_size()
            scale = min(2.0, 2200 / max(width, height), (4_000_000 / (width * height)) ** .5)
            with closing(page.render(scale=scale)) as bitmap:
                image = bitmap.to_pil().convert('RGB')
                image.save(target, 'JPEG', quality=85)
    return {'page': index}


if __name__ == '__main__':
    # Linux worker limit; timeout is enforced by the parent on every platform.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (75, 75))
        if sys.platform == 'linux':
            resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)
    except (ImportError, ValueError):
        pass
    try:
        if sys.argv[1] == 'inspect':
            data = _inspect(sys.argv[2], int(sys.argv[3]))
        else:
            data = _render(sys.argv[2], int(sys.argv[3]), sys.argv[4])
        print(json.dumps(data, ensure_ascii=False))
    except Exception:
        sys.exit(1)
