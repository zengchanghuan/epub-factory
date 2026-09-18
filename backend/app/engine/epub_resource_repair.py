"""Bounded, deterministic repair of local EPUB resource defects.

Never changes the source archive or invents missing prose/images. Recover exact
HTML filename aliases first; otherwise insert explicit notices and retain every
available chapter. Unknown missing resource types remain fatal.
"""
from dataclasses import dataclass, field
import mimetypes
import posixpath
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from lxml import etree, html

from .epub_compat import OPF_NS, EPUB_NS, is_font_item, missing_font_is_optional, xml_tree
from .font_compat import FONT_FACE

WARNING_META = 'epub-factory-source-warning'
MISSING_DOCUMENT_ATTRIBUTE = 'data-epub-factory-missing-document'
WARNING_ALIASES = '原文件存在资源文件名不一致，已恢复可用文件并修正引用。'
WARNING_IMAGES = '原文件缺少部分图片，已用明确缺图说明占位，其余内容继续处理。'
WARNING_STYLES = '原文件缺少部分样式，已使用阅读器默认样式继续处理。'
WARNING_DOCUMENTS = '原文件缺少部分章节，已插入缺章说明页；说明页不代表译文。'
WARNING_NAVIGATION = '原文件目录资源缺失，已根据现有章节重建目录。'
WARNING_MANIFEST = '原文件部分资源未在清单声明，已保留现有资源并补齐声明。'
KNOWN_WARNINGS = frozenset({WARNING_ALIASES, WARNING_IMAGES, WARNING_STYLES,
                          WARNING_DOCUMENTS, WARNING_NAVIGATION, WARNING_MANIFEST})
HTML_TYPES = {'application/xhtml+xml', 'text/html'}
NCX_TYPE = 'application/x-dtbncx+xml'
MAX_ENTRIES = 50_000
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
INVALID = 'EPUB 文件结构损坏或资源路径不安全，无法继续处理。'
MISSING_BODY = '原文件没有可读取的正文章节，请提供包含正文的 EPUB。'
MISSING_RESOURCE = '原文件缺少无法安全恢复的资源，请提供完整 EPUB。'
_CSS_URL = re.compile(r'''url\(\s*(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|([^\s)'";]+))\s*\)''', re.I)
_CSS_IMPORT = re.compile(r'@import\s+(?:' + _CSS_URL.pattern + r'''|"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)')''', re.I)
_CSS_ESCAPE = re.compile(r'\\([0-9a-fA-F]{1,6})(?:\s)?|\\([^\n\r])')


class ResourceRepairError(ValueError):
    """Only safe fixed messages are suitable for an upload error."""


@dataclass
class ResourceRepairPlan:
    replacements: dict[str, bytes] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    readable_body_count: int = 0


def _local(source, reference):
    parsed = urlsplit(reference.strip())
    if parsed.scheme or parsed.netloc:
        return None
    path = unquote(parsed.path)
    if not path:
        return source
    if '\\' in path or '\x00' in path:
        raise ResourceRepairError(INVALID)
    target = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    if target == '..' or target.startswith(('../', '/')):
        raise ResourceRepairError(INVALID)
    return target


def _relative(source, target, original=''):
    parsed = urlsplit(original)
    return urlunsplit(('', '', quote(posixpath.relpath(target, posixpath.dirname(source)), safe='/'),
                       parsed.query, parsed.fragment))


def _document(raw):
    try:
        tree = xml_tree(raw)
        dtd = tree.getroottree().docinfo.internalDTD
        if dtd is not None and any(dtd.iterentities()):
            raise ResourceRepairError(INVALID)
        return tree
    except etree.XMLSyntaxError:
        return html.fromstring(raw, parser=html.HTMLParser(no_network=True))


def _placeholder(fragments):
    root = etree.Element('{http://www.w3.org/1999/xhtml}html', nsmap={None: 'http://www.w3.org/1999/xhtml'})
    head = etree.SubElement(root, '{http://www.w3.org/1999/xhtml}head')
    etree.SubElement(head, '{http://www.w3.org/1999/xhtml}title').text = '原文件缺少本章节'
    body = etree.SubElement(root, '{http://www.w3.org/1999/xhtml}body', {
        MISSING_DOCUMENT_ATTRIBUTE: 'true', 'translate': 'no'})
    section = etree.SubElement(body, '{http://www.w3.org/1999/xhtml}section')
    etree.SubElement(section, '{http://www.w3.org/1999/xhtml}h1').text = '原文件缺少本章节'
    etree.SubElement(section, '{http://www.w3.org/1999/xhtml}p').text = '本页仅说明原文件缺失，不代表译文；其余可用章节继续处理。'
    for fragment in sorted(fragments):
        etree.SubElement(section, '{http://www.w3.org/1999/xhtml}span', id=fragment)
    return etree.tostring(root, encoding='utf-8', xml_declaration=True)


_MISSING_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="640" height="120" viewBox="0 0 640 120"><title>原文件缺少此图片</title><rect width="640" height="120" fill="#f5f5f5" stroke="#999"/><text x="320" y="68" text-anchor="middle" font-size="24" fill="#555">原文件缺少此图片</text></svg>'''.encode('utf-8')


def build_resource_repair_plan(archive, opf_path):
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES or sum(info.file_size for info in entries) > MAX_TOTAL_BYTES:
        raise ResourceRepairError('EPUB 解压内容过大，暂时无法安全检查，请拆分后上传。')
    members = {}
    seen = set()
    for info in entries:
        name = info.filename
        if (name in seen or '\\' in name or '\x00' in name or name.startswith('/')
                or posixpath.normpath(name) == '..' or posixpath.normpath(name).startswith('../')):
            raise ResourceRepairError(INVALID)
        seen.add(name)
        if not info.is_dir():
            members[name] = info
    plan = ResourceRepairPlan()
    names = set(members)

    def read(name, limit=MAX_DOCUMENT_BYTES):
        if name in plan.replacements:
            return plan.replacements[name]
        info = members.get(name)
        if info is None or info.file_size > limit:
            raise ResourceRepairError(INVALID)
        with archive.open(info) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ResourceRepairError(INVALID)
        return raw

    raw_opf = read(opf_path, MAX_METADATA_BYTES)
    package = _document(raw_opf)
    manifest, spine = package.find('{*}manifest'), package.find('{*}spine')
    if manifest is None or spine is None or not len(spine):
        raise ResourceRepairError(INVALID)
    changed = False
    warnings = set()
    for meta in package.findall('.//{*}meta'):
        if meta.get('name') == WARNING_META and meta.get('content') in KNOWN_WARNINGS:
            warnings.add(meta.get('content'))
    by_path, by_id = {}, {}
    missing_docs, missing_navigation, image_targets = {}, [], {}
    documents, styles, ncx = set(), set(), set()

    def alias(path):
        stem, suffix = posixpath.splitext(path)
        if suffix.lower() not in {'.html', '.xhtml', '.htm'}:
            return None
        matches = [stem + ext for ext in ('.html', '.xhtml', '.htm') if stem + ext in names]
        return matches[0] if len(matches) == 1 else None

    def declare(path, kind):
        nonlocal changed
        if path in by_path:
            return by_path[path]
        uid = 'epub-factory-resource-' + str(len(by_id))
        while uid in by_id:
            uid += '-r'
        item = etree.SubElement(manifest, f'{{{OPF_NS}}}item', {
            'id': uid, 'href': _relative(opf_path, path), 'media-type': kind})
        by_path[path] = item
        by_id[uid] = item
        changed = True
        return item

    # Register the complete original manifest before fixing cross references.
    for item in manifest:
        uid, href = item.get('id'), item.get('href')
        if not uid or uid in by_id or not href:
            raise ResourceRepairError(INVALID)
        path = _local(opf_path, href)
        if path is None:
            raise ResourceRepairError(INVALID)
        by_id[uid] = item
        by_path[path] = item

    for item in list(manifest):
        path = _local(opf_path, item.get('href'))
        kind = item.get('media-type', '')
        is_nav = 'nav' in item.get('properties', '').split()
        if path not in names and kind in HTML_TYPES:
            recovered = alias(path)
            if recovered:
                item.set('href', _relative(opf_path, recovered))
                by_path.pop(path, None)
                by_path[recovered] = item
                path = recovered
                warnings.add(WARNING_ALIASES)
                changed = True
        if path not in names:
            if kind in HTML_TYPES:
                if is_nav:
                    missing_navigation.append((path, kind))
                else:
                    missing_docs.setdefault(path, set())
                    warnings.add(WARNING_DOCUMENTS)
            elif kind == NCX_TYPE:
                missing_navigation.append((path, kind))
            elif kind == 'text/css':
                plan.replacements[path] = b'/* Original stylesheet was missing; use reader defaults. */\n'
                warnings.add(WARNING_STYLES)
            elif kind.startswith('image/'):
                # A real SVG placeholder has a matching filename and media type.
                target = posixpath.join(posixpath.dirname(opf_path), f'epub-factory-missing-image-{len(image_targets)}.svg')
                while target in names:
                    target = target[:-4] + '-r.svg'
                image_targets[path] = target
                plan.replacements[target] = _MISSING_SVG
                item.set('href', _relative(opf_path, target))
                item.set('media-type', 'image/svg+xml')
                by_path.pop(path, None); by_path[target] = item
                names.add(target)
                warnings.add(WARNING_IMAGES)
                changed = True
                path = target
            elif kind == 'application/oebps-page-map+xml' or missing_font_is_optional(package, item):
                continue
            else:
                raise ResourceRepairError(MISSING_RESOURCE)
            names.update(plan.replacements)
        if kind in HTML_TYPES:
            documents.add(path)
        elif kind == 'text/css':
            styles.add(path)
        elif kind == NCX_TYPE:
            ncx.add(path)
        elif kind == 'image/svg+xml' and path in names:
            documents.add(path)

    # Require at least one actual readable spine document before adding notices.
    trees = {}
    for ref in spine:
        item = by_id.get(ref.get('idref'))
        if item is None:
            raise ResourceRepairError(INVALID)
        path = _local(opf_path, item.get('href', ''))
        if path not in members or 'nav' in item.get('properties', '').split() or item.get('media-type') not in HTML_TYPES | {'image/svg+xml'}:
            continue
        if path not in trees:
            trees[path] = _document(read(path))
        tree = trees[path]
        bodies = tree.xpath('//*[local-name()="body"]')
        body = bodies[0] if bodies else tree
        readable_text = ''.join(body.xpath('.//text()[not(ancestor::*[local-name()="script" or local-name()="style"])]')).strip()
        if (body.get(MISSING_DOCUMENT_ATTRIBUTE) == 'true'
                and re.sub(r'\s+', '', readable_text) == '原文件缺少本章节本页仅说明原文件缺失，不代表译文；其余可用章节继续处理。'):
            continue
        has_image = any(_local(path, el.get('src') or el.get('href') or '') in members
                        for el in body.xpath('.//*[local-name()="img" or local-name()="image"]'))
        has_svg_drawing = bool(body.xpath('.//*[local-name()="path" or local-name()="rect" or local-name()="circle" or local-name()="polygon" or local-name()="line"]')) if item.get('media-type') == 'image/svg+xml' else False
        if readable_text or has_image or has_svg_drawing:
            plan.readable_body_count += 1
    if not plan.readable_body_count:
        raise ResourceRepairError(MISSING_BODY)

    def resource(source, value, kind):
        target = _local(source, value)
        if target is None:
            return value
        original_target = target
        if kind == 'document' and target not in names:
            recovered = alias(target)
            if recovered:
                target = recovered
                warnings.add(WARNING_ALIASES)
            elif posixpath.splitext(target)[1].lower() in {'.html', '.xhtml', '.htm'}:
                missing_docs.setdefault(target, set())
                fragment = unquote(urlsplit(value).fragment)
                if fragment:
                    missing_docs[target].add(fragment)
                declare(target, 'application/xhtml+xml')
                warnings.add(WARNING_DOCUMENTS)
            else:
                return value
        if kind == 'document' and target in names and target not in by_path:
            if posixpath.splitext(target)[1].lower() in {'.html', '.xhtml', '.htm'}:
                declare(target, 'application/xhtml+xml')
                documents.add(target)
                warnings.add(WARNING_MANIFEST)
        if kind == 'image':
            if target in image_targets:
                target = image_targets[target]
            elif target not in names:
                replacement = posixpath.join(posixpath.dirname(opf_path), f'epub-factory-missing-image-{len(image_targets)}.svg')
                while replacement in names:
                    replacement = replacement[:-4] + '-r.svg'
                image_targets[target] = replacement
                plan.replacements[replacement] = _MISSING_SVG
                names.add(replacement)
                target = replacement
                warnings.add(WARNING_IMAGES)
            if target not in by_path:
                guessed = 'image/svg+xml' if target.endswith('.svg') else mimetypes.guess_type(target)[0]
                if not guessed or not guessed.startswith('image/'):
                    header = read(target)[:16]
                    guessed = ('image/png' if header.startswith(b'\x89PNG') else 'image/jpeg' if header.startswith(b'\xff\xd8') else None)
                if not guessed:
                    raise ResourceRepairError(MISSING_RESOURCE)
                declare(target, guessed)
                if target in members:
                    warnings.add(WARNING_MANIFEST)
                if guessed == 'image/svg+xml':
                    documents.add(target)
        elif kind == 'stylesheet':
            if target not in names:
                plan.replacements[target] = b'/* Original stylesheet was missing; use reader defaults. */\n'
                names.add(target)
                warnings.add(WARNING_STYLES)
            if target not in by_path:
                declare(target, 'text/css')
                if target in members:
                    warnings.add(WARNING_MANIFEST)
            styles.add(target)
        return _relative(source, target, value) if target != original_target else value

    def css(source, text):
        # Keep comments and font-face payload byte-equivalent. Fonts already
        # have a dedicated fallback repair at packaging time.
        protected, imports = [], []

        def hold(values, prefix, value):
            token = '\x00' + prefix + str(len(values)) + '\x00'
            values.append(value)
            return token

        text = re.sub(r'/\*.*?\*/', lambda m: hold(protected, 'KEEP', m[0]), text, flags=re.S)
        text = FONT_FACE.sub(lambda m: hold(protected, 'KEEP', m[0]), text)

        def replace(match, kind):
            raw_value = next((part for part in match.groups() if part is not None), '')
            decoded = _CSS_ESCAPE.sub(lambda m: chr(int(m[1], 16)) if m[1] else m[2], raw_value)
            new = resource(source, decoded, kind)
            return match[0] if new == decoded else match[0].replace(raw_value, new, 1)

        text = _CSS_IMPORT.sub(lambda m: hold(imports, 'IMPORT', replace(m, 'stylesheet')), text)
        text = _CSS_URL.sub(lambda m: replace(m, 'image'), text)
        for n, value in enumerate(imports):
            text = text.replace('\x00IMPORT' + str(n) + '\x00', value)
        # Restore in reverse order because a held font-face can contain a held
        # comment. All payloads and whitespace remain exactly as supplied.
        for n in reversed(range(len(protected))):
            text = text.replace('\x00KEEP' + str(n) + '\x00', protected[n])
        return text

    # Preserve original document bytes unless a concrete reference changes.
    # A valid EPUB3 navigation supersedes stale NCX links; ebooklib regenerates
    # that compatibility NCX from the retained nav, so do not invent notices for
    # obsolete NCX-only targets that are not part of the actual reading order.
    has_nav = False
    for nav_item in manifest:
        if 'nav' not in nav_item.get('properties', '').split():
            continue
        nav_path = _local(opf_path, nav_item.get('href', ''))
        if nav_path in members:
            if nav_path not in trees:
                trees[nav_path] = _document(read(nav_path))
            nav_tree = trees[nav_path]
            has_nav = bool(nav_tree.xpath('//*[local-name()="nav"][@*[local-name()="type"]="toc"]/*[local-name()="ol"]')) or has_nav
    if has_nav:
        ncx.clear()
    checked = set()
    missing_nav_paths = {path for path, _kind in missing_navigation}
    while (documents | ncx) - checked:
        path = next(iter((documents | ncx) - checked))
        checked.add(path)
        if path in missing_docs or path in missing_nav_paths:
            continue
        tree = trees.pop(path, None)
        if tree is None:
            tree = _document(read(path))
        edited = False
        for node in tree.iter():
            if not isinstance(node.tag, str):
                continue
            tag = node.tag.rsplit('}', 1)[-1].rsplit(':', 1)[-1].lower()
            for attribute, value in list(node.attrib.items()):
                local = attribute.rsplit('}', 1)[-1].rsplit(':', 1)[-1].lower()
                kind = None
                if tag in {'img', 'image'} and local in {'src', 'href'}:
                    kind = 'image'
                elif tag == 'video' and local == 'poster':
                    kind = 'image'
                elif tag == 'object' and local == 'data' and node.get('type', '').startswith('image/'):
                    kind = 'image'
                elif tag == 'input' and local == 'src' and node.get('type', '').lower() == 'image':
                    kind = 'image'
                elif tag == 'link' and local == 'href' and 'stylesheet' in node.get('rel', '').lower().split():
                    kind = 'stylesheet'
                elif (tag == 'a' and local == 'href') or (tag == 'content' and local == 'src'):
                    kind = 'document'
                if kind:
                    value = resource(path, value, kind)
                elif local == 'style':
                    value = css(path, value)
                elif local == 'srcset' and tag in {'img', 'source'}:
                    value = re.sub(r'(?:^|,\s*)(data:[^\s]+|[^,\s]+)(?:\s+[^,]*)?',
                        lambda m: m[0].replace(m[1].rstrip(','), resource(path, m[1].rstrip(','), 'image'), 1), value)
                if value != node.get(attribute):
                    node.set(attribute, value); edited = True
            if tag == 'style' and node.text:
                value = css(path, node.text)
                if value != node.text:
                    node.text = value; edited = True
        if edited:
            plan.replacements[path] = etree.tostring(tree, encoding='utf-8', xml_declaration=True)

    checked_styles = set()
    while styles - checked_styles:
        path = next(iter(styles - checked_styles)); checked_styles.add(path)
        raw = read(path)
        original_text = raw.decode('utf-8', errors='replace')
        changed_text = css(path, original_text)
        if changed_text != original_text:
            plan.replacements[path] = changed_text.encode('utf-8')
    for path, fragments in missing_docs.items():
        plan.replacements[path] = _placeholder(fragments)
    for path, kind in missing_navigation:
        links = []
        for index, ref in enumerate(spine):
            item = by_id.get(ref.get('idref'))
            if item is not None and 'nav' not in item.get('properties', '').split():
                links.append((_relative(path, _local(opf_path, item.get('href'))), f'章节 {index + 1}'))
        if kind == NCX_TYPE:
            root = etree.Element('{http://www.daisy.org/z3986/2005/ncx/}ncx', version='2005-1', nsmap={None: 'http://www.daisy.org/z3986/2005/ncx/'})
            head = etree.SubElement(root, 'head'); etree.SubElement(head, 'meta', name='dtb:depth', content='1')
            etree.SubElement(etree.SubElement(root, 'docTitle'), 'text').text = '目录'
            listing = etree.SubElement(root, 'navMap')
            for n, (href, title) in enumerate(links, 1):
                node = etree.SubElement(listing, 'navPoint', id=f'nav-{n}', playOrder=str(n))
                etree.SubElement(etree.SubElement(node, 'navLabel'), 'text').text = title
                etree.SubElement(node, 'content', src=href)
        else:
            root = etree.Element('{http://www.w3.org/1999/xhtml}html', nsmap={None: 'http://www.w3.org/1999/xhtml', 'epub': EPUB_NS})
            etree.SubElement(etree.SubElement(root, 'head'), 'title').text = '目录'
            body = etree.SubElement(root, 'body')
            listing = etree.SubElement(etree.SubElement(body, 'nav', {f'{{{EPUB_NS}}}type': 'toc'}), 'ol')
            for href, title in links:
                etree.SubElement(etree.SubElement(listing, 'li'), 'a', href=href).text = title
        plan.replacements[path] = etree.tostring(root, encoding='utf-8', xml_declaration=True)
        warnings.add(WARNING_NAVIGATION)
    if warnings:
        metadata = package.find('{*}metadata')
        if metadata is None:
            raise ResourceRepairError(INVALID)
        previous = {node.get('content') for node in metadata if node.get('name') == WARNING_META}
        for warning in sorted(warnings - previous):
            etree.SubElement(metadata, f'{{{OPF_NS}}}meta', name=WARNING_META, content=warning)
            changed = True
    plan.warnings = sorted(warnings)
    if changed:
        plan.replacements[opf_path] = etree.tostring(package, encoding='utf-8', xml_declaration=True)
    return plan
