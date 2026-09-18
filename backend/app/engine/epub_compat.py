"""Normalize EPUB representation without changing or inventing book content."""
from copy import deepcopy
from pathlib import PurePosixPath
import posixpath
import re
from types import MethodType
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from ebooklib import epub
from lxml import etree, html
from .html_compat import upgrade_legacy_html

OPF_NS = 'http://www.idpf.org/2007/opf'
EPUB_NS = 'http://www.idpf.org/2007/ops'
DC_NS = 'http://purl.org/dc/elements/1.1/'
XML_NS = 'http://www.w3.org/XML/1998/namespace'
CONTAINER_NS = 'urn:oasis:names:tc:opendocument:xmlns:container'
DC_TERMS = {'title', 'creator', 'subject', 'description', 'publisher', 'contributor',
            'date', 'type', 'format', 'identifier', 'source', 'language', 'relation',
            'coverage', 'rights'}
# The bundled EPUBCheck 5.1.0 PackageVocabs defines these default vocabularies.
# Declared namespaced extension properties must remain untouched.
ITEM_PROPERTIES = {'nav', 'cover-image', 'mathml', 'scripted', 'svg', 'switch', 'remote-resources',
                   'data-nav', 'dictionary', 'glossary', 'index', 'search-key-map'}
META_PROPERTIES = {'alternate-script', 'authority', 'belongs-to-collection', 'collection-type',
                   'display-seq', 'dictionary-type', 'file-as', 'group-position', 'identifier-type',
                   'meta-auth', 'role', 'source-language', 'source-of', 'target-language', 'term',
                   'title-type', 'pageBreakSource'}
FONT_MEDIA_TYPES = {'application/vnd.ms-opentype', 'application/font-sfnt',
                    'application/font-woff', 'application/x-font-ttf',
                    'application/x-font-opentype', 'application/x-font-truetype'}


def is_font_item(item):
    kind = (item.get('media-type') or '').lower()
    return kind.startswith('font/') or kind in FONT_MEDIA_TYPES


def missing_font_is_optional(package, item):
    """Only remove an absent reading font without OPF references/dependencies.

CSS font-face sources are handled separately by the packager's existing font
fallback repair. Spine/fallback/overlay/refines/metadata dependencies are not
guessed away. Shared with the pre-payment gate to keep both decisions aligned.
"""
    uid = item.get('id')
    if not is_font_item(item) or not uid or item.get('properties'):
        return False
    if any(item.get(key) for key in ('fallback', 'fallback-style', 'media-overlay')):
        return False
    target = posixpath.normpath(unquote(urlsplit(item.get('href') or '').path))
    for node in package.iter():
        if node is item:
            continue
        for key, value in node.attrib.items():
            local = key.rsplit('}', 1)[-1]
            if local == 'id':
                continue
            if uid in value.split() or '#' + uid in value.split():
                return False
            if local in {'href', 'src', 'content'}:
                link = urlsplit(value)
                if not link.scheme and not link.netloc and link.path:
                    if posixpath.normpath(unquote(link.path)) == target:
                        return False
    return True


def xml_tree(raw):
    return etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))


def selected_package_path(container):
    """Match ebooklib's last declared OPF, including the container namespace."""
    roots = [item for item in container.iter(f'{{{CONTAINER_NS}}}rootfile')
             if item.get('media-type') == 'application/oebps-package+xml']
    if not roots:
        raise ValueError('EPUB 缺少有效 OPF 入口，请提供完整 EPUB。')
    path = roots[-1].get('full-path') or ''
    uri = urlsplit(path)
    if (not path or uri.scheme or uri.netloc or uri.query or uri.fragment
            or '\\' in path or '\x00' in path or path.startswith('/')
            or posixpath.normpath(path) == '..' or posixpath.normpath(path).startswith('../')):
        raise ValueError('EPUB 的 OPF 入口无效，请提供完整 EPUB。')
    return path


def package_path(archive):
    return selected_package_path(xml_tree(archive.read('META-INF/container.xml')))


def valid_xml_id(value):
    try: return bool(value) and etree.QName(value).namespace is None
    except ValueError: return False


def normalize_package(archive, opf_path):
    """Repair types and remove only optional absent page maps/reading fonts."""
    raw = archive.read(opf_path)
    tree = xml_tree(raw)
    manifest = tree.find(f'{{{OPF_NS}}}manifest')
    spine = tree.find(f'{{{OPF_NS}}}spine')
    if manifest is None or spine is None: raise ValueError('EPUB 缺少 manifest 或 spine')
    base = posixpath.dirname(opf_path)
    names = set(archive.namelist())
    changed = False
    ids = [item.get('id') for item in manifest]
    if len(ids) != len(set(ids)): raise ValueError('原书 manifest 存在重复资源标识，无法可靠确定章节引用。')
    # Decide before ID repair so even legacy numeric refines/dependencies stay
    # visible to the guard. Removing the declaration prevents ebooklib from
    # unconditionally reading the missing font before CSS fallback can run.
    for item in list(manifest):
        resource = posixpath.normpath(posixpath.join(base, unquote(item.get('href') or '')))
        if resource not in names and is_font_item(item):
            if not missing_font_is_optional(tree, item):
                raise ValueError('原书缺少被其他资源依赖的字体文件，请提供完整 EPUB。')
            manifest.remove(item)
            changed = True
    used_ids = {node.get('id') for node in tree.iter() if node.get('id')}
    remapped = {}
    for number, item in enumerate(manifest):
        old_id = item.get('id')
        if valid_xml_id(old_id): continue
        new_id = f'item-compat-{number}'
        while new_id in used_ids: new_id += '-compat'
        used_ids.add(new_id); item.set('id', new_id)
        if old_id: remapped[old_id] = new_id
        changed = True
    if remapped:
        for node in tree.iter():
            for key in ['idref', 'toc', 'page-map', 'fallback', 'media-overlay']:
                if node.get(key) in remapped: node.set(key, remapped[node.get(key)])
            if node.get('name') == 'cover' and node.get('content') in remapped:
                node.set('content', remapped[node.get('content')])
    for item in list(manifest):
        href = unquote(item.get('href') or '')
        resource = posixpath.normpath(posixpath.join(base, href))
        media_type = item.get('media-type') or ''
        properties = (item.get('properties') or '').split()
        obsolete = [value for value in properties if ':' not in value and value not in ITEM_PROPERTIES]
        if obsolete:
            kept = [value for value in properties if value not in obsolete]
            if kept: item.set('properties', ' '.join(kept))
            else: item.attrib.pop('properties', None)
            metadata = tree.find(f'{{{OPF_NS}}}metadata')
            if metadata is not None:
                etree.SubElement(metadata, f'{{{OPF_NS}}}meta', {
                    'name': 'legacy-resource-properties-' + item.get('id'), 'content': ' '.join(obsolete)})
            changed = True
        if resource not in names:
            if media_type == 'application/oebps-page-map+xml':
                if spine.get('page-map') == item.get('id'): spine.attrib.pop('page-map', None)
                manifest.remove(item); changed = True
                continue
            if media_type in {'application/xhtml+xml', 'text/html'}:
                raise ValueError(f'原书缺少正文/目录资源：{PurePosixPath(resource).name}；请提供完整 EPUB，不能凭空补齐正文。')
            raise ValueError('原书缺少声明的资源文件，请提供完整 EPUB。')
        if media_type == 'text/html' and PurePosixPath(href).suffix.lower() in {'.html', '.htm', '.xhtml'}:
            item.set('media-type', 'application/xhtml+xml'); changed = True
        if media_type.startswith('image/') and resource in names:
            with archive.open(resource) as image: header = image.read(16)
            actual = ('image/png' if header.startswith(b'\x89PNG\r\n\x1a\n') else
                      'image/jpeg' if header.startswith(b'\xff\xd8\xff') else
                      'image/gif' if header.startswith((b'GIF87a', b'GIF89a')) else
                      'image/webp' if header.startswith(b'RIFF') and header[8:12] == b'WEBP' else None)
            if actual and actual != media_type:
                item.set('media-type', actual); changed = True
    nav = next((i for i in manifest if 'nav' in (i.get('properties') or '').split()), None)
    prefer_nav = False
    if nav is not None:
        resource = posixpath.normpath(posixpath.join(base, unquote(nav.get('href') or '')))
        if resource in names:
            node = html.fromstring(archive.read(resource))
            prefer_nav = bool(node.xpath('//nav[@*="toc"]/ol'))
    return (etree.tostring(tree, encoding='utf-8', xml_declaration=True) if changed else raw), prefer_nav


def normalize_metadata(book, raw):
    """Upgrade legacy metadata without dropping author/identifier information.

    ebooklib emits EPUB3 but copies EPUB2 DC attributes and converts Calibre
    meta entries into foreign XML elements. Rebuild the metadata representation
    from the OPF, retaining extensions as compatible name/content entries.
    """
    tree = xml_tree(raw)
    source = tree.find(f'{{{OPF_NS}}}metadata')
    if source is None: return
    prefix_map = dict(re.findall(r'([\w.-]+):\s+(\S+)', tree.get('prefix') or ''))
    for prefix, uri in prefix_map.items():
        if prefix != 'rendition': book.add_prefix(prefix, uri)
    metadata = {DC_NS: {}, OPF_NS: {'meta': []}}
    extras = metadata[OPF_NS]['meta']
    used_ids = {node.get('id') for node in source if node.get('id')}

    def compatible(name, value):
        extras.append((None, {'name': name, 'content': value or ''}))

    for number, node in enumerate(source):
        if not isinstance(node.tag, str): continue
        qname = etree.QName(node)
        attrs = dict(node.attrib)
        if qname.namespace == DC_NS and qname.localname in DC_TERMS:
            keep = {k: v for k, v in attrs.items() if k in {'id', 'dir', f'{{{XML_NS}}}lang'}}
            refinements = []
            for key, value in attrs.items():
                if key in keep: continue
                local = etree.QName(key).localname
                if local in {'file-as', 'role'} or (local == 'scheme' and qname.localname == 'identifier'):
                    refinements.append(('identifier-type' if local == 'scheme' else local, value))
                else: compatible(f'legacy-dc-{qname.localname}-{local}', value)
            if refinements and not keep.get('id'):
                uid = f'legacy-meta-{number}'
                while uid in used_ids: uid += '-compat'
                used_ids.add(uid); keep['id'] = uid
            metadata[DC_NS].setdefault(qname.localname, []).append((node.text, keep))
            for property_name, value in refinements:
                extras.append((value, {'refines': '#' + keep['id'], 'property': property_name}))
        elif qname.namespace == OPF_NS and qname.localname == 'meta':
            # Keep extension meta as OPF meta; do not reinterpret name prefixes
            # as XML element namespaces, as ebooklib's reader normally does.
            property_name = attrs.get('property') or ''
            if property_name and ':' not in property_name and property_name not in META_PROPERTIES:
                # An undeclared custom property has no default-vocabulary
                # meaning. Preserve it as legacy metadata, not invalid EPUB3.
                legacy = {'name': property_name, 'content': node.text or attrs.get('content') or ''}
                if attrs.get('id'): legacy['id'] = attrs['id']
                extras.append((None, legacy))
                for key, value in attrs.items():
                    if key not in {'property', 'id', 'content'}:
                        compatible(f'legacy-meta-{property_name}-{etree.QName(key).localname}', value)
            else:
                extras.append((node.text, attrs))
        else:
            prefix = node.prefix or qname.namespace or 'extension'
            compatible(f'{prefix}:{qname.localname}', node.text)
            for key, value in attrs.items():
                compatible(f'{prefix}:{qname.localname}:{etree.QName(key).localname}', value)
    book.metadata = metadata
    languages = metadata[DC_NS].get('language', [])
    if languages and languages[0][0]: book.language = languages[0][0]


def _preserved_get_content(item, default=None):
    """ebooklib discards root/body attributes, direct body text and head nodes."""
    output = item._epub_factory_original_get_content(default)
    if not output or not item.content: return output
    try:
        original = xml_tree(item.content)
    except etree.XMLSyntaxError:
        original = html.fromstring(item.content)
    generated = xml_tree(output)
    for attribute, value in original.attrib.items():
        # HTML fallback can expose unexpanded xml:/epub: attributes.
        if ':' in attribute and not attribute.startswith('{'):
            prefix, local = attribute.split(':', 1)
            ns = {'xml': 'http://www.w3.org/XML/1998/namespace', 'epub': EPUB_NS}.get(prefix)
            if not ns: continue
            attribute = f'{{{ns}}}{local}'
        generated.set(attribute, value)
    old_body = original.xpath('//*[local-name()="body"]')
    new_body = generated.xpath('//*[local-name()="body"]')
    # ebooklib parses XHTML through an HTML parser. HTML raw-text elements
    # keep XML entities literally, which would escape them again on write.
    # Restore their parsed source payload, never unescape arbitrary strings.
    for section in ('head', 'body'):
        old_sections = original.xpath(f'//*[local-name()="{section}"]')
        new_sections = generated.xpath(f'//*[local-name()="{section}"]')
        if not old_sections or not new_sections: continue
        selector = './/*[local-name()="script" or local-name()="style"]'
        old_raw = old_sections[0].xpath(selector)
        new_raw = new_sections[0].xpath(selector)
        if len(old_raw) != len(new_raw) or not all(
            etree.QName(a).localname == etree.QName(b).localname
            and a.get('id') == b.get('id') for a, b in zip(old_raw, new_raw)
        ): continue
        for source, target in zip(old_raw, new_raw):
            target.text = source.text
            for child in list(target): target.remove(child)
            for child in source: target.append(deepcopy(child))
    if old_body and new_body:
        for attribute, value in old_body[0].attrib.items():
            if ':' in attribute and not attribute.startswith('{'):
                prefix, local = attribute.split(':', 1)
                ns = {'xml': 'http://www.w3.org/XML/1998/namespace', 'epub': EPUB_NS}.get(prefix)
                if not ns: continue
                attribute = f'{{{ns}}}{local}'
            new_body[0].set(attribute, value)
        new_body[0].text = old_body[0].text
        # Text directly inside body otherwise has no leaf block to translate.
        # Wrap the same text (no additions/deletions) with stable paragraph
        # nodes so extraction and Reduce see the same complete document.
        body = new_body[0]
        ns = etree.QName(body).namespace
        p_tag = f'{{{ns}}}p' if ns else 'p'
        if body.text and body.text.strip():
            paragraph = etree.Element(p_tag, {'class': 'epub-factory-direct-text'})
            paragraph.text = body.text; body.text = None; body.insert(0, paragraph)
        for child in list(body):
            if child.tail and child.tail.strip():
                paragraph = etree.Element(p_tag, {'class': 'epub-factory-direct-text'})
                paragraph.text = child.tail; child.tail = None
                body.insert(body.index(child) + 1, paragraph)
    old_head = original.xpath('//*[local-name()="head"]')
    new_head = generated.xpath('//*[local-name()="head"]')
    if old_head and new_head:
        def signature(child):
            local = etree.QName(child).localname
            attrs = dict(child.attrib)
            if local == 'link' and attrs.get('rel') == 'stylesheet' and attrs.get('type') == 'text/css':
                attrs.pop('type')  # HTML stylesheet's default MIME type.
            return (local, tuple(sorted(attrs.items())), (child.text or '').strip(),
                    tuple(signature(c) for c in child if isinstance(c.tag, str)))
        signatures = {signature(child) for child in new_head[0] if isinstance(child.tag, str)}
        for child in old_head[0]:
            if not isinstance(child.tag, str): continue
            local = etree.QName(child).localname
            if local == 'title' and new_head[0].xpath('./*[local-name()="title"]'): continue
            key = signature(child)
            if key not in signatures:
                new_head[0].append(deepcopy(child)); signatures.add(key)
    # Repair invalid source meta value= while preserving its metadata value.
    for node in generated.xpath('//*[local-name()="head"]/*[local-name()="meta"]'):
        if 'value' in node.attrib:
            if 'content' not in node.attrib: node.set('content', node.get('value'))
            del node.attrib['value']
    # The writer applies book.language; an inherited lang/xml:lang disagreement
    # introduced during serialization is invalid and confuses screen readers.
    for node in generated.iter():
        if not isinstance(node.tag, str): continue
        xml_lang = node.get(f'{{{XML_NS}}}lang')
        if xml_lang is not None and node.get('lang') is not None:
            node.set('lang', xml_lang)
    upgrade_legacy_html(generated, getattr(item.book, 'title', '') or '')
    return etree.tostring(generated, encoding='utf-8', xml_declaration=True)


def normalize_book(book, opf_path):
    """Keep every resource inside a canonical package-relative namespace."""
    base = posixpath.dirname(opf_path)
    items = list(book.get_items())
    use_archive_root = any(posixpath.normpath(item.get_name()).startswith('../') for item in items)
    old_to_new, archive_to_new = {}, {}
    old_nav_bases, old_ncx_bases = [], []
    for item in items:
        old = item.get_name().replace('\\', '/')
        physical = posixpath.normpath(posixpath.join(base, old))
        new = physical if use_archive_root else posixpath.normpath(old)
        if new.startswith('../') or new.startswith('/'):
            raise ValueError('EPUB 资源路径超出容器边界')
        if physical in archive_to_new:
            raise ValueError('原书多个资源指向同一文件，无法可靠重打包。')
        old_to_new[posixpath.normpath(old)] = new
        archive_to_new[physical] = new
        if isinstance(item, epub.EpubNav): old_nav_bases.append(posixpath.dirname(physical))
        if isinstance(item, epub.EpubNcx): old_ncx_bases.append(posixpath.dirname(physical))
        item.file_name = new
        if isinstance(item, epub.EpubHtml) and not hasattr(item, '_epub_factory_original_get_content'):
            item._epub_factory_original_get_content = item.get_content
            item.get_content = MethodType(_preserved_get_content, item)

    def rewrite(href):
        parsed = urlsplit(href or '')
        if parsed.scheme or parsed.netloc or not parsed.path: return href
        path = unquote(parsed.path).replace('\\', '/')
        key = posixpath.normpath(path)
        target = old_to_new.get(key)
        if target is None:
            candidates = {archive_to_new[p] for directory in [base, *old_nav_bases, *old_ncx_bases]
                          if (p := posixpath.normpath(posixpath.join(directory, path))) in archive_to_new}
            if len(candidates) == 1: target = candidates.pop()
        if target is None: return href
        return urlunsplit(('', '', quote(target, safe='/'), parsed.query, parsed.fragment))

    def toc(nodes):
        for node in nodes or []:
            obj = node[0] if isinstance(node, tuple) else node
            if getattr(obj, 'href', None): obj.href = rewrite(obj.href)
            if isinstance(node, tuple): toc(node[1])
    toc(book.toc)
    for page in getattr(book, 'pages', []):
        if getattr(page, 'href', None): page.href = rewrite(page.href)
    for guide in book.guide:
        if guide.get('href'): guide['href'] = rewrite(guide['href'])
    return book
