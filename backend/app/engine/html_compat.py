"""Upgrade obsolete HTML presentation without changing prose or anchors."""
import re
from lxml import etree


def upgrade_legacy_html(root, book_title=''):
    def css(node, property_name, value):
        style = node.get('style') or ''
        if not re.search(r'(?:^|;)\s*' + re.escape(property_name) + r'\s*:', style, re.I):
            node.set('style', style.rstrip(';') + f';{property_name}:{value}')

    for node in root.iter():
        if not isinstance(node.tag, str): continue
        if any(etree.QName(parent).localname in {'svg', 'math'} for parent in [node, *node.iterancestors()] if isinstance(parent.tag, str)):
            continue
        local = etree.QName(node).localname
        legacy_type = node.attrib.pop('epub-type', None)
        if legacy_type is not None:
            semantic_type = '{http://www.idpf.org/2007/ops}type'
            if semantic_type not in node.attrib:
                node.set(semantic_type, legacy_type)
            elif node.get(semantic_type) != legacy_type:
                node.set('data-legacy-epub-type', legacy_type)
        if local == 'title' and not ''.join(node.itertext()).strip(): node.text = book_title or 'Untitled'
        if local == 'font':
            namespace = etree.QName(node).namespace
            node.tag = f'{{{namespace}}}span' if namespace else 'span'
            for old, new in [('face', 'font-family'), ('color', 'color')]:
                value = node.attrib.pop(old, None)
                if value: css(node, new, value)
            value = node.attrib.pop('size', None)
            if value and re.fullmatch(r'[+-]?\d+', value):
                number = int(value) + (3 if value.startswith(('+', '-')) else 0)
                sizes = ['xx-small', 'x-small', 'small', 'medium', 'large', 'x-large', 'xx-large']
                css(node, 'font-size', sizes[max(1, min(7, number)) - 1])
        alignment = node.attrib.pop('align', None)
        if alignment:
            alignment = alignment.lower()
            if local in {'img', 'table'} and alignment in {'left', 'right'}: css(node, 'float', alignment)
            elif local == 'table' and alignment == 'center':
                css(node, 'margin-left', 'auto'); css(node, 'margin-right', 'auto')
            elif local == 'img' and alignment in {'top', 'middle', 'bottom'}: css(node, 'vertical-align', alignment)
            elif alignment in {'left', 'right', 'center', 'justify'}: css(node, 'text-align', alignment)
        for dimension in ['width', 'height']:
            value = (node.get(dimension) or '').strip()
            if value and local not in {'img', 'object', 'video', 'iframe', 'canvas', 'table', 'td', 'th', 'col', 'colgroup', 'hr'}:
                # Paragraph/blockquote dimensions are not HTML presentation
                # attributes. Turning legacy width=0pt into CSS collapses
                # readable prose; retain the unknown hint without inventing
                # layout semantics.
                node.set('data-legacy-' + dimension, node.attrib.pop(dimension))
                continue
            if local in {'img', 'object', 'video', 'iframe', 'canvas'} and re.fullmatch(r'\d+', value): continue
            if value and (re.fullmatch(r'\d+(?:\.\d+)?(?:%|px|em|rem|pt|pc|in|cm|mm|q|ex|ch|vw|vh|vmin|vmax)?', value, re.I) or value == 'auto'):
                css(node, dimension, value + ('px' if re.fullmatch(r'\d+(?:\.\d+)?', value) else ''))
                node.attrib.pop(dimension, None)
