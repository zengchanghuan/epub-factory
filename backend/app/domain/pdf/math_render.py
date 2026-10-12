"""Offline TeX presentation. No browser scripts, TeX executable or remote assets.

Keep the source in accessible text and private IR. Rendering never validates or
repairs mathematics; unsupported input retains a link to the original page.
"""
import hashlib
import html
import re
import threading
import xml.etree.ElementTree as ET

_lock = threading.Lock()
_math = re.compile(r'(?<!\\)\$\$(.+?)\$\$|(?<!\\)\$([^$\n]+)\$|\\\((.+?)\\\)|\\\[(.+?)\\\]', re.S)
_cjk = re.compile(r'[\u3400-\u9fff]')
_nodes = {'math', 'mrow', 'mi', 'mo', 'mn', 'mtext', 'mspace', 'msup', 'msub', 'msubsup',
          'mfrac', 'msqrt', 'mroot', 'mover', 'munder', 'munderover', 'mtable', 'mtr', 'mtd',
          'mstyle', 'mpadded', 'mphantom', 'menclose', 'mfenced', 'mmultiscripts', 'mprescripts', 'none'}


def _escape(text):
    return html.escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text), quote=True)


class MathAssets:
    def __init__(self, *, include_source_pages=True):
        self.assets = {}
        self.cache = {}
        self.report = {'svg': 0, 'mathml': 0, 'source_fallback': 0}
        self.include_source_pages = include_source_pages

    def inline(self, text, page, paragraph_tag=None):
        def prose(value):
            escaped = _escape(value)
            if paragraph_tag is not None:
                escaped = escaped.replace('\n\n', '</p>' + paragraph_tag).replace('\n', '<br/>')
            return escaped
        parts, end = [], 0
        for match in _math.finditer(text):
            tex = next(value for value in match.groups() if value is not None)
            # Do not interpret pairs of currency amounts as math delimiters.
            if match.group(2) is not None and re.match(r'^\d+(?:[.,]\d+)?\s+[^+*/=^_{}\\]+$', tex):
                continue
            parts.append(prose(text[end:match.start()]))
            parts.append(self.formula(tex, page, display=bool(match.group(1) or match.group(4))))
            end = match.end()
        parts.append(prose(text[end:]))
        return ''.join(parts)

    def equation(self, text, page):
        matches = list(_math.finditer(text))
        if matches:
            return self.inline(text, page)
        # Spaces/underscores in an OCR imitation of a stacked calculation are
        # positional evidence, not a license to guess a fraction or alignment.
        if '\n' in text or re.search(r'_{3,}', text) or (_cjk.search(text) and '\\' not in text):
            return self.fallback(text, page)
        return self.formula(text, page, display=True)

    def fallback(self, text, page):
        self.report['source_fallback'] += 1
        reference = (f' <a class="pdf-math-reference" href="#original-page-{page}">公式原页对照</a>'
                     if self.include_source_pages else ' <span class="pdf-math-reference">（公式排版待核验）</span>')
        return f'<code class="pdf-math">{_escape(text)}</code>' + reference

    def formula(self, tex, page, display=False):
        key = (tex, display)
        if key not in self.cache:
            try:
                self.cache[key] = self._render(tex, display)
            except Exception:
                # Broken TeX must never abort an otherwise recoverable book.
                self.cache[key] = None
        rendered = self.cache[key]
        if rendered is None:
            return self.fallback(tex, page)
        kind, markup = rendered
        self.report[kind] += 1
        if kind == 'mathml' and self.include_source_pages:
            markup += f' <a class="pdf-math-reference" href="#original-page-{page}">原页</a>'
        return markup

    def _render(self, tex, display):
        if len(tex) > 2048 or not tex.strip():
            raise ValueError('math input limit')
        depth = 0
        for token in re.findall(r'(?<!\\)[{}]', tex):
            depth += 1 if token == '{' else -1
            if not 0 <= depth <= 32:
                raise ValueError('math nesting limit')
        if depth:
            raise ValueError('unbalanced math')
        import latex2mathml.converter
        import ziamath
        # latex2mathml 3.81.1 loses the upright variant in \\mathrm{x} unless
        # whitespace follows the command (also handled by ziamath's TeX helper).
        layout_tex = re.sub(r'\\mathrm(?=\{)', lambda _: '\\mathrm ', tex)
        tree = ET.fromstring(latex2mathml.converter.convert(layout_tex, display='block' if display else 'inline'))
        nodes = list(tree.iter())
        if len(nodes) > 1000 or any(node.tag.rsplit('}', 1)[-1] not in _nodes for node in nodes):
            raise ValueError('unsupported math markup')
        if any('\\' in (node.text or '') for node in nodes):
            raise ValueError('unknown TeX command')
        for node in nodes:
            if any(key.lower().startswith('on') or key in {'href', 'src', 'style'} for key in node.attrib):
                raise ValueError('unsafe math attribute')
        with _lock:
            previous = ziamath.config.svg2
            try:
                ziamath.config.svg2 = False  # EPUB SVG 1.1; paths embed the font.
                drawing = ziamath.Math(tree, size=24)
                # The bundled math font has no CJK. Use reader-native MathML
                # instead of quietly emitting blank glyphs in an SVG image.
                missing = any(not drawing.font.glyphindex(char) for node in nodes
                              for char in node.text or '' if not char.isspace() and char not in '\u2061\u2062\u2063')
                if missing:
                    for node in nodes:
                        node.tag = node.tag.rsplit('}', 1)[-1]
                    tree.set('xmlns', 'http://www.w3.org/1998/Math/MathML')
                    tree.set('alttext', tex)
                    return 'mathml', ET.tostring(tree, encoding='unicode')
                svg = drawing.svg()
            finally:
                ziamath.config.svg2 = previous
        root = ET.fromstring(svg)
        width, height = float(root.get('width')), float(root.get('height'))
        if len(svg) > 1_000_000 or not 0 < width < 20000 or not 0 < height < 20000:
            raise ValueError('math output limit')
        for node in root.iter():
            if node.tag.rsplit('}', 1)[-1] in {'script', 'foreignObject', 'image'}:
                raise ValueError('external SVG content')
            if any(key.rsplit('}', 1)[-1] in {'href', 'src'} and not value.startswith('#')
                   for key, value in node.attrib.items()):
                raise ValueError('external SVG reference')
        name = 'math/' + hashlib.sha256((str(display) + '\0' + tex).encode()).hexdigest()[:24] + '.svg'
        self.assets[name] = svg
        # Scale with the reader's font setting; viewBox keeps radicals/fractions
        # intact. Use the SVG baseline for inline descenders and subscripts.
        _, y, _, h = map(float, root.get('viewBox').split())
        align = -(y+h)/24
        return 'svg', (f'<img class="pdf-math-image" src="{name}" alt="{_escape(tex)}"'
                       f' style="width:{width/24:.4f}em;height:auto;vertical-align:{align:.4f}em;"/>')
