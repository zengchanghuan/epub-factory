"""Repair only navigation whose intended destination has local evidence.

Keep prose, note links, valid destinations and the source archive untouched.
Unknown/ambiguous targets stay invalid for EPUBCheck rather than being guessed.
"""
from dataclasses import dataclass, field
from collections import Counter
import hashlib
import posixpath
import re
import unicodedata
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import ebooklib
import cssutils
from ebooklib import epub
from lxml import etree


WARNING_REPAIRED_NAVIGATION = '原文件部分目录锚点失效，已根据目标章节的可验证标题或唯一锚点恢复跳转。'
WARNING_DISABLED_NAVIGATION = '原文件目录含无目标的占位链接，已保留其文字并停用无效跳转。'
WARNING_UNRESOLVED_NAVIGATION = '原文件部分目录目标无法可靠恢复，须通过最终校验后才能交付。'
WARNING_META = 'epub-factory-source-warning'
ORIGINAL_HREF = 'data-epub-factory-original-href'
EPUB_NS = 'http://www.idpf.org/2007/ops'
HEADINGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
NOTE_TYPES = {'noteref', 'footnote', 'endnote', 'rearnote', 'backlink'}


@dataclass
class NavigationRepairReport:
    repaired: int = 0
    disabled: int = 0
    unresolved: int = 0
    warnings: list[str] = field(default_factory=list)


def _name(node):
    return etree.QName(node).localname if isinstance(node.tag, str) else ''


def _label(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _is_note(node):
    for item in [node, *node.iterancestors()]:
        types = set((item.get(f'{{{EPUB_NS}}}type') or item.get('epub:type') or '').split())
        roles = set((item.get('role') or '').split())
        if types & NOTE_TYPES or roles & {'doc-noteref', 'doc-backlink', 'doc-footnote', 'doc-endnote'}:
            return True
    return False


def _outside_toc(node):
    for item in [node, *node.iterancestors()]:
        types = set((item.get(f'{{{EPUB_NS}}}type') or '').split())
        if types & {'page-list', 'landmarks'} or item.get('role') == 'doc-pagelist':
            return True
    return False


def _heading_label(node, superscript_styles):
    """Ignore only one demonstrable trailing superscript numeric annotation.

    This is label comparison only: the marker remains in the document. In
    particular, an ordinary trailing digit (Chapter 1) is never discarded.
    """
    children = list(node)
    if children:
        marker = children[-1]
        value = ''.join(marker.itertext()).strip()
        inline = cssutils.parseStyle(marker.get('style', ''))
        aligned = inline.getPropertyValue('vertical-align')
        matched_values = set()
        for name in marker.get('class', '').split():
            matched_values.update((superscript_styles or {}).get('.' + name, set()))
            matched_values.update((superscript_styles or {}).get(_name(marker) + '.' + name, set()))
        is_super = _name(marker) == 'sup' or (not inline.getPropertyValue('all') and (
            (aligned == 'super' and superscript_styles is not None)
            or (not aligned and matched_values == {'super'})))
        if is_super and re.fullmatch(r'[0-9]+', value) and not (marker.tail or '').strip():
            return (node.text or '') + ''.join(
                etree.tostring(child, encoding='unicode', method='text') for child in children[:-1])
    return ''.join(node.itertext())


def _superscript_classes(root, path, items):
    """Recognize simple local CSS class declarations without fetching imports."""
    styles = {}
    parser = cssutils.CSSParser(validate=False, fetcher=lambda _url: (None, None))
    sheets = []
    for node in root.iter():
        if _name(node) == 'style':
            if node.get('media', '').strip().lower() not in {'', 'all'} or node.get('disabled') is not None:
                return None
            sheets.append(''.join(node.itertext()))
        elif _name(node) == 'link' and 'stylesheet' in node.get('rel', '').split():
            if node.get('media', '').strip().lower() not in {'', 'all'} or node.get('disabled') is not None:
                return None
            target = _destination(path, node.get('href', ''))
            item = items.get(target[0]) if target else None
            if item is not None and item.get_type() == ebooklib.ITEM_STYLE:
                sheets.append(item.get_content())
            else:
                return None
    for raw in sheets:
        text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
        if re.search(r'@import\b', text, re.IGNORECASE):
            return None
        for rule in parser.parseString(raw):
            if re.search(r'\ball\s*:', rule.cssText, re.IGNORECASE):
                return None
            if rule.type == rule.IMPORT_RULE or (
                    rule.type != rule.STYLE_RULE and 'vertical-align' in rule.cssText.lower()):
                return None  # Unknown/conditional cascades are not proof.
            if rule.type != rule.STYLE_RULE:
                continue
            alignment = rule.style.getPropertyValue('vertical-align')
            if not alignment:
                continue
            for selector in rule.selectorText.split(','):
                matched = re.fullmatch(r'(?:[a-zA-Z]+)?\.[\w-]+', selector.strip())
                if not matched or rule.style.getPropertyPriority('vertical-align'):
                    return None  # Do not invent a CSS cascade engine here.
                styles.setdefault(selector.strip(), set()).add(alignment)
    return styles


def _destination(source, href):
    parsed = urlsplit(href or '')
    if parsed.scheme or parsed.netloc:
        return None
    path = unquote(parsed.path)
    if '\\' in path or '\x00' in path:
        return None
    target = posixpath.normpath(posixpath.join(posixpath.dirname(source), path)) if path else source
    if target.startswith('/') or target == '..' or target.startswith('../'):
        return None
    return target, unquote(parsed.fragment), parsed


def _toc_nodes(nodes):
    for entry in nodes or []:
        node = entry[0] if isinstance(entry, (tuple, list)) else entry
        yield node
        if isinstance(entry, (tuple, list)):
            yield from _toc_nodes(entry[1])


def normalize_book_navigation(book):
    """Synchronize HTML TOCs, ebooklib TOC/NCX and OPF guide destinations.

    Missing fragments can use an exact unique heading. Explicit reading-start
    landmarks can use a unique heading at the very beginning of the document.
    A TOC guide may use its document start. Only explicit X-filled placeholder
    links inside an HTML TOC are disabled; ordinary links are never erased.
    """
    report = NavigationRepairReport()
    items = {posixpath.normpath(item.get_name()): item for item in book.get_items()
             if item.get_name()}
    documents = {}
    for path, item in items.items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        try:
            raw = item.get_content()
            if isinstance(raw, str):
                raw = raw.encode('utf-8')
            root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
        except (etree.XMLSyntaxError, TypeError, ValueError):
            continue  # A separate structural gate remains authoritative.
        documents[path] = root
    if not documents:
        return report

    nav_documents = {path for path, item in items.items() if isinstance(item, epub.EpubNav)}
    declared_tocs = set(nav_documents)
    guide_tocs = set()
    for guide in getattr(book, 'guide', []):
        target = _destination('', guide.get('href', ''))
        if guide.get('type') == 'toc' and target and target[0] in documents:
            declared_tocs.add(target[0])
            guide_tocs.add(target[0])

    ids = {path: {node.get('id') for node in root.iter() if node.get('id')}
           for path, root in documents.items()}
    id_counts = {path: Counter(node.get('id') for node in root.iter() if node.get('id'))
                 for path, root in documents.items()}
    references = []
    # Each reference retains its original base, label and owner; no global
    # string replacement can accidentally alter prose, scripts or note links.
    for path, root in documents.items():
        scopes = [root] if path in guide_tocs and path not in nav_documents else [
            node for node in root.iter() if _name(node) == 'nav' and (
                'toc' in (node.get(f'{{{EPUB_NS}}}type') or '').split()
                or node.get('role') == 'doc-toc')]
        seen = set()
        for scope in scopes:
            for link in scope.iter():
                if (_name(link) != 'a' or not link.get('href') or _is_note(link)
                        or _outside_toc(link) or link in seen):
                    continue
                seen.add(link)
                references.append((path, link.get('href'), ''.join(link.itertext()), 'html', link))
    for node in _toc_nodes(getattr(book, 'toc', [])):
        if getattr(node, 'href', ''):
            references.append(('', node.href, getattr(node, 'title', ''), 'toc', node))
    for guide in getattr(book, 'guide', []):
        # Notes/other landmarks are not chapter or directory navigation.
        if guide.get('type') in {'toc', 'text', 'bodymatter'} and guide.get('href'):
            references.append(('', guide['href'], guide.get('title', ''), 'guide', guide))

    changed_documents = set()
    css_superscripts = {}
    verified_destinations = {}

    def choose_anchor(path, title):
        root = documents[path]
        headings = [node for node in root.iter() if _name(node) in HEADINGS and not _is_note(node)]
        exact = [node for node in headings if _label(title)
                 and _label(''.join(node.itertext())) == _label(title)]
        if not exact and any(len(node) for node in headings):
            if path not in css_superscripts:
                css_superscripts[path] = _superscript_classes(root, path, items)
            exact = [node for node in headings if _label(title)
                     and _label(_heading_label(node, css_superscripts[path])) == _label(title)]
        if len(exact) == 1:
            chosen = exact[0]
            anchor = chosen.get('id')
            if anchor and id_counts[path][anchor] != 1:
                return None
            if not anchor:
                anchor = 'epub-factory-nav-' + hashlib.sha256(
                    (path + '\n' + _label(title)).encode()).hexdigest()[:16]
                while anchor in ids[path]:
                    anchor += '-nav'
                chosen.set('id', anchor)
                ids[path].add(anchor)
                id_counts[path][anchor] += 1
                changed_documents.add(path)
            return anchor
        return None

    def reading_start_anchor(path, owner):
        if owner.get('type') not in {'text', 'bodymatter'} or _label(owner.get('title')) not in {
                'start', 'beginning', 'bodymatter', '正文', '开始', '開始'}:
            return None
        root = documents[path]
        headings = [node for node in root.iter() if _name(node) in HEADINGS and not _is_note(node)]
        if (len(headings) != 1 or ids[path] != {headings[0].get('id')}
                or id_counts[path][headings[0].get('id')] != 1):
            return None
        heading = headings[0]
        body = next((node for node in root.iter() if _name(node) == 'body'), None)
        if body is None or not ''.join(heading.itertext()).strip():
            return None
        # The unique anchor must be the first visible body text, not a later
        # subsection after an unanchored introduction.
        first = next((text for text in body.xpath('.//text()') if text.strip()), None)
        if first is None or first.getparent() not in {heading, *heading.iterdescendants()}:
            return None
        return heading.get('id')

    for source, href, title, kind, owner in references:
        target = _destination(source, href)
        if target is None:
            continue
        path, fragment, parsed = target
        if path not in items:
            # EpubNav is regenerated from book.toc by ebooklib; disabling only
            # its in-memory XHTML would falsely claim the bad target is gone.
            if kind == 'html' and source not in nav_documents and re.fullmatch(r'[xX]{4,}', posixpath.basename(path)):
                owner.set(ORIGINAL_HREF, href)
                owner.set('aria-disabled', 'true')
                del owner.attrib['href']
                changed_documents.add(source)
                report.disabled += 1
            else:
                report.unresolved += 1
            continue
        if not fragment or path not in documents or fragment in ids[path]:
            continue
        anchor = choose_anchor(path, title)
        if anchor is None and kind == 'guide':
            candidates = verified_destinations.get((path, fragment), set())
            anchor = next(iter(candidates)) if len(candidates) == 1 else reading_start_anchor(path, owner)
        if (anchor is None and kind == 'guide' and owner.get('type') == 'toc'
                and path in declared_tocs and not ids[path]):
            anchor = ''  # The guide identifies this exact TOC document.
        if anchor is None:
            report.unresolved += 1
            continue
        repaired_href = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                    parsed.query, quote(anchor, safe='-._~:')))
        if kind == 'html':
            owner.set('href', repaired_href)
            changed_documents.add(source)
        elif kind == 'guide':
            owner['href'] = repaired_href
        else:
            owner.href = repaired_href
        if kind in {'html', 'toc'}:
            verified_destinations.setdefault((path, fragment), set()).add(anchor)
        report.repaired += 1

    for path in changed_documents:
        items[path].set_content(etree.tostring(documents[path], encoding='utf-8', xml_declaration=True))
    if report.repaired:
        report.warnings.append(WARNING_REPAIRED_NAVIGATION)
    if report.disabled:
        report.warnings.append(WARNING_DISABLED_NAVIGATION)
    if report.unresolved:
        report.warnings.append(WARNING_UNRESOLVED_NAVIGATION)
    existing = {attrs.get('content') for _value, attrs in book.get_metadata('OPF', 'meta')
                if attrs.get('name') == WARNING_META}
    for warning in report.warnings:
        if warning not in existing:
            book.add_metadata('OPF', 'meta', '', {'name': WARNING_META, 'content': warning})
    return report
