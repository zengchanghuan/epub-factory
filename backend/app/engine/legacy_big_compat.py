"""Conservative EPUB 2 <big> migration, without implementing a CSS cascade.

Only selector-invariant stylesheets are accepted. Unknown imports, conditional
rules, type-sensitive selectors, and uncertain font declarations retain <big>
so the normal EPUBCheck gate can report the unsupported input. No CSS is fetched.
"""
import re

import cssutils
from lxml import etree


_HTML = 'http://www.w3.org/1999/xhtml'
_SELECTOR = re.compile(r'(\*|[A-Za-z][\w-]*)?((?:[.#][A-Za-z_][\w-]*)*)\Z')
_FONT = {'font', 'font-size', 'all'}
_WIDE = {'inherit', 'initial', 'unset'}


def _name(node):
    if not isinstance(node.tag, str):
        return ''
    qname = etree.QName(node)
    if qname.namespace not in {None, _HTML}:
        return ''
    if any(isinstance(p.tag, str) and etree.QName(p).localname in {'svg', 'math'}
           for p in node.iterancestors()):
        return ''
    return qname.localname


def _font_state(style, parser):
    """Return explicit font presence and whether its meaning is uncertain."""
    try:
        declaration = parser.parseStyle(style or '')
    except Exception:
        return False, True
    mentioned = {m.lower() for m in re.findall(r'(?:^|;)\s*([\w-]+)\s*:', style or '')} & _FONT
    parsed, explicit, uncertain = set(), False, False
    for prop in declaration.getProperties(all=True):
        if prop.name.lower() not in _FONT:
            continue
        parsed.add(prop.name.lower())
        value = prop.value.lower().strip()
        # revert would restore the different UA defaults of big and span.
        valid = value in _WIDE or prop.name != 'all' and prop.valid
        if not valid or re.search(r'\b(?:var|env)\s*\(|\brevert(?:-layer)?\b', value):
            uncertain = True
        else:
            explicit = True
    return explicit, uncertain or bool(mentioned - parsed)


def upgrade_legacy_big(root, stylesheet_loader=None):
    candidates = [n for n in root.iter() if _name(n) == 'big']
    if not candidates:
        return
    parser = cssutils.CSSParser(validate=False, raiseExceptions=True,
                               fetcher=lambda _url: (None, None))
    rules = []
    for node in root.iter():
        name = _name(node)
        if name in {'style', 'link'} and node.get('type', 'text/css').split(';', 1)[0].strip().lower() not in {'', 'text/css'}:
            continue
        if name == 'style':
            raw = ''.join(node.itertext())
        elif name == 'link' and 'stylesheet' in node.get('rel', '').lower().split():
            try:
                raw = stylesheet_loader(node.get('href', '')) if stylesheet_loader else None
            except Exception:
                return
            if raw is None:
                return
        else:
            continue
        if (node.get('media', '').strip().lower() not in {'', 'all'}
                or node.get('disabled') is not None or node.get('title')):
            return
        try:
            raw = raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw
            # Never resolve @import or infer behavior of opaque modern at-rules.
            if re.search(r'@import\b', raw, re.I):
                return
            sheet = parser.parseString(raw)
            for rule in sheet.cssRules:
                if rule.type in {rule.COMMENT, rule.CHARSET_RULE, rule.FONT_FACE_RULE, rule.PAGE_RULE}:
                    continue
                if rule.type != rule.STYLE_RULE:
                    return
                for selector in rule.selectorText.split(','):
                    match = _SELECTOR.fullmatch(selector.strip())
                    if not match or not selector.strip():
                        return
                    tag, suffix = match.groups()
                    # Renaming must neither lose big rules nor gain span rules.
                    # Complex structural selectors can affect sibling nodes too.
                    if tag and tag.lower() in {'big', 'span'}:
                        return
                    rules.append((tag, re.findall(r'([.#])([\w-]+)', suffix),
                                  _font_state(rule.style.cssText, parser)))
        except Exception:
            return
    plan = []
    for node in candidates:
        explicit, uncertain = _font_state(node.get('style'), parser)
        for tag, suffix, (has_font, unknown_font) in rules:
            if tag and tag != '*' and tag.lower() != 'big':
                continue
            classes = set(node.get('class', '').split())
            if any((kind == '.' and value not in classes) or
                   (kind == '#' and value != node.get('id')) for kind, value in suffix):
                continue
            explicit |= has_font
            uncertain |= unknown_font
        if uncertain:
            return  # All-or-nothing, including nested big elements.
        plan.append((node, explicit))
    for node, explicit in plan:
        namespace = etree.QName(node).namespace
        node.tag = f'{{{namespace}}}span' if namespace else 'span'
        if not explicit:
            # Equivalent to big's UA default; each nested node scales relative
            # to its parent. Never override an author's font/font-size/all rule.
            style = node.get('style') or ''
            separator = ';' if style and not style.rstrip().endswith(';') else ''
            node.set('style', style + separator + 'font-size:larger')
