"""Upgrade obsolete HTML presentation without changing prose or anchors."""
import re
import cssutils
from lxml import etree


_SIDES = ('top', 'right', 'bottom', 'left')
_TABLE_PARTS = {'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'col', 'colgroup'}


def _table_property(name):
    return (name in {'all', 'padding', 'border', 'border-width', 'border-spacing', 'vertical-align'}
            or name.startswith(('padding-', 'border-inline', 'border-block'))
            or name in {f'border-{side}' for side in _SIDES}
            or name in {f'border-{side}-width' for side in _SIDES})


def _mentioned_table_properties(style):
    return {name.lower() for name in re.findall(r'(?:^|[;{])\s*([\w-]+)\s*:', style or '')
            if _table_property(name.lower())}


def _html_name(node):
    if not isinstance(node.tag, str):
        return ''
    if any(etree.QName(parent).localname in {'svg', 'math'}
           for parent in [node, *node.iterancestors()] if isinstance(parent.tag, str)):
        return ''
    return etree.QName(node).localname


def _style_properties(style):
    declaration = cssutils.CSSParser(validate=False).parseStyle(style or '')
    known, uncertain = set(), set()
    for prop in declaration.getProperties():
        if not _table_property(prop.name.lower()):
            continue
        # cssutils validates CSS 2.1; explicitly recognize later CSS-wide
        # keywords without treating arbitrary invalid values as author rules.
        valid = prop.value.lower() in {'initial', 'inherit', 'unset'} or prop.valid
        (known if valid else uncertain).add(prop.name.lower())
    mentioned = _mentioned_table_properties(style)
    uncertain.update(mentioned - known)
    return known, uncertain


def _simple_selector_matches(selector, node):
    """Only a statically provable subset; unknown selectors return None."""
    matched = re.fullmatch(r'(\*|[A-Za-z][\w-]*)?((?:[.#][\w-]+)*)', selector.strip())
    if not matched or not selector.strip():
        return None
    tag, suffix = matched.groups()
    if tag and tag != '*' and tag.lower() != _html_name(node):
        return False
    classes = set(node.get('class', '').split())
    for kind, name in re.findall(r'([.#])([\w-]+)', suffix):
        if kind == '.' and name not in classes or kind == '#' and node.get('id') != name:
            return False
    return True


class _TableAuthorStyles:
    """Detect declarations, not computed values or a replacement CSS cascade.

    Any known matching author declaration outranks HTML presentation hints.
    When relevance cannot be proven, keep the obsolete attribute so EPUBCheck
    blocks delivery instead of silently promoting/dropping a layout hint.
    """
    def __init__(self, root, stylesheet_loader):
        self.rules = []
        self.uncertain = set()
        self.unknown = False
        parser = cssutils.CSSParser(validate=False, fetcher=lambda _url: (None, None))
        for node in root.iter():
            name = _html_name(node)
            if name == 'style':
                if node.get('type', 'text/css').split(';', 1)[0].strip().lower() not in {'', 'text/css'}:
                    continue
                raw = ''.join(node.itertext())
            elif name == 'link' and 'stylesheet' in node.get('rel', '').lower().split():
                if node.get('type', 'text/css').split(';', 1)[0].strip().lower() not in {'', 'text/css'}:
                    continue  # e.g. Adobe page templates are not CSS sheets.
                raw = stylesheet_loader(node.get('href', '')) if stylesheet_loader else None
                if raw is None:
                    self.unknown = True
                    continue
            else:
                continue
            try:
                raw = raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw
            except UnicodeDecodeError:
                self.unknown = True
                continue
            if re.search(r'@import\b', raw, re.I):
                self.unknown = True
                continue
            conditional = node.get('media', '').strip().lower() not in {'', 'all'} or node.get('disabled') is not None
            try:
                rules = parser.parseString(raw)
            except Exception:
                self.unknown = True
                continue

            def collect(rules, conditional=False):
                for rule in rules:
                    if rule.type == rule.NAMESPACE_RULE:
                        self.unknown = True  # The simple selector subset has no namespace resolver.
                    elif rule.type == rule.STYLE_RULE:
                        properties, uncertain = _style_properties(rule.style.cssText)
                        if conditional:
                            self.uncertain.update(properties | uncertain)
                        else:
                            for selector in rule.selectorText.split(','):
                                self.rules.append((selector.strip(), properties, uncertain))
                    elif hasattr(rule, 'cssRules'):
                        collect(rule.cssRules, True)
                    else:
                        # cssutils keeps newer @supports/@layer/@scope blocks
                        # as opaque rules. They are not evidence of absence.
                        self.uncertain.update(_mentioned_table_properties(rule.cssText))
            collect(rules, conditional)

    def properties(self, node):
        known, uncertain = _style_properties(node.get('style'))
        uncertain.update(self.uncertain)
        if self.unknown:
            uncertain.add('all')
        for selector, properties, invalid in self.rules:
            matches = _simple_selector_matches(selector, node)
            if matches is None:
                uncertain.update(properties | invalid)
            elif matches:
                known.update(properties)
                uncertain.update(invalid)
        return known, uncertain


def _conflicts(properties, property_name):
    if 'all' in properties or property_name in properties:
        return True
    if property_name.startswith('padding-'):
        return 'padding' in properties
    if property_name.startswith('border-') and property_name.endswith('-width'):
        side = property_name.split('-')[1]
        return bool(properties & {'border', 'border-width', f'border-{side}'})
    return False


def _logical_conflict(properties, property_name):
    if not (property_name.startswith('padding-') or
            property_name.startswith('border-') and property_name.endswith('-width')):
        return False
    family = 'padding' if property_name.startswith('padding-') else 'border'
    return any(
        prop.startswith((family + '-inline', family + '-block')) for prop in properties)


def _append_style(node, property_name, value):
    style = node.get('style') or ''
    separator = ';' if style and not style.rstrip().endswith(';') else ''
    node.set('style', style + separator + f'{property_name}:{value}')


def _upgrade_legacy_tables(root, stylesheet_loader):
    candidates = [node for node in root.iter() if _html_name(node) in _TABLE_PARTS]
    if not any(any(attr in node.attrib for attr in ('cellpadding', 'cellspacing', 'border', 'valign'))
               or _html_name(node) == 'table' and any(_html_name(child) == 'col' for child in node)
               for node in candidates):
        return
    needs_styles = any(any(attr in node.attrib for attr in ('cellpadding', 'cellspacing', 'border', 'valign'))
                       for node in candidates)
    styles = _TableAuthorStyles(root, stylesheet_loader) if needs_styles else None

    def migrate(owner, attribute, changes):
        raw = owner.get(attribute)
        if raw is None:
            return
        owner.set('data-legacy-' + attribute, raw)
        if changes is None:
            return  # Unsupported value keeps the structural delivery gate shut.
        pending = []
        for node, prop, value in changes:
            known, uncertain = styles.properties(node)
            if (_conflicts(uncertain, prop) or _logical_conflict(uncertain | known, prop)):
                return
            if not _conflicts(known, prop):
                pending.append((node, prop, value))
        for node, prop, value in pending:
            _append_style(node, prop, value)
        del owner.attrib[attribute]

    for node in candidates:
        local = _html_name(node)
        if local == 'table':
            cells = [cell for cell in node.iter() if _html_name(cell) in {'td', 'th'} and
                     next((parent for parent in cell.iterancestors() if _html_name(parent) == 'table'), None) is node]
            for attribute in ('cellpadding', 'cellspacing'):
                raw = node.get(attribute)
                if raw is None:
                    continue
                value = raw.strip()
                changes = None
                if re.fullmatch(r'\d+', value):
                    pixels = str(int(value)) + 'px'
                    changes = ([(cell, f'padding-{side}', pixels) for cell in cells for side in _SIDES]
                               if attribute == 'cellpadding' else [(node, 'border-spacing', pixels)])
                migrate(node, attribute, changes)
            raw = node.get('border')
            if raw is not None:
                changes = ([(node, f'border-{side}-width', '0px') for side in _SIDES]
                           if re.fullmatch(r'0+', raw.strip()) else None)
                migrate(node, 'border', changes)
        if node.get('valign') is not None:
            value = node.get('valign').strip().lower()
            changes = ([(node, 'vertical-align', value)] if local in {'td', 'th'}
                       and value in {'top', 'middle', 'bottom', 'baseline'} else None)
            migrate(node, 'valign', changes)

    # Move existing col nodes, including attributes/IDs/tails, without cloning
    # or regrouping across intervening rows/captions/other element siblings.
    for table in candidates:
        if _html_name(table) != 'table':
            continue
        group = None
        for child in list(table):
            if _html_name(child) == 'col':
                if group is None:
                    namespace = etree.QName(table).namespace
                    group = etree.Element(f'{{{namespace}}}colgroup' if namespace else 'colgroup')
                    table.insert(table.index(child), group)
                table.remove(child)
                group.append(child)
            else:
                group = None


def upgrade_legacy_html(root, book_title='', *, stylesheet_loader=None):
    _upgrade_legacy_tables(root, stylesheet_loader)
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
