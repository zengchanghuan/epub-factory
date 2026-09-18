"""Bounded, local-only resource checks before an EPUB can create a paid order.

This is not EPUBCheck: legacy metadata, optional fonts/page maps and repairable
markup remain supported. Text is never returned, logged, or sent to a model.
"""
import posixpath
import re
import zipfile
from urllib.parse import unquote, urlsplit

from lxml import etree

from app.engine.font_compat import FONT_FACE
from app.engine.epub_compat import missing_font_is_optional, selected_package_path

MAX_ENTRIES = 50_000
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
HTML_TYPES = {'application/xhtml+xml', 'text/html'}
NCX_TYPE = 'application/x-dtbncx+xml'
INVALID = 'EPUB 文件结构损坏或不受支持，请上传完整、可正常打开的 EPUB。'
TOO_LARGE = 'EPUB 解压内容过大，暂时无法安全检查，请拆分后上传。'
MISSING_TEXT = '原书缺少正文或目录文件，请上传完整 EPUB 后再支付。'
MISSING_IMAGE = '原书缺少图片资源，请上传完整 EPUB 后再支付。'
MISSING_RESOURCE = '原书缺少必需资源文件，请上传完整 EPUB 后再支付。'
UNDECLARED_RESOURCE = '原书引用了未完整声明的资源，请修复 EPUB 后再支付。'
_CSS_URL = re.compile(r'''url\(\s*(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|([^\s)'";]+))\s*\)''', re.I)
_CSS_IMPORT = re.compile(r'@import\s+(?:' + _CSS_URL.pattern + r'''|"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)')''', re.I)
_CSS_ESCAPE = re.compile(r'\\([0-9a-fA-F]{1,6})(?:\s)?|\\([^\n\r])')


class EpubInputError(ValueError):
    """Only fixed, public-safe diagnostics cross the upload boundary."""


def _local_path(source, reference):
    parsed = urlsplit(reference.strip())
    if parsed.scheme or parsed.netloc:
        return None  # No external URI is ever fetched by this check.
    path = unquote(parsed.path)
    if not path:
        return source
    if '\\' in path or '\x00' in path:
        raise EpubInputError(INVALID)
    result = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    if result == '..' or result.startswith(('../', '/')):
        raise EpubInputError(INVALID)
    return result


def _css_references(text):
    # Missing optional fonts already have a safe reading-font fallback.
    text = FONT_FACE.sub('', re.sub(r'/\*.*?\*/', '', text, flags=re.S))
    for kind, pattern, source in (('stylesheet', _CSS_IMPORT, text),
                                   ('image', _CSS_URL, _CSS_IMPORT.sub(' ', text))):
        for match in pattern.finditer(source):
            value = next((part for part in match.groups() if part is not None), '')
            yield kind, _CSS_ESCAPE.sub(lambda m: chr(int(m[1], 16)) if m[1] else m[2], value)


def _srcset_urls(value):
    # Data URIs may contain commas; descriptors end at the next comma.
    for match in re.finditer(r'(?:^|,\s*)(data:[^\s]+|[^,\s]+)(?:\s+[^,]*)?', value):
        yield match[1].rstrip(',')


class _References:
    """libxml's event target avoids building a DOM or indexing book text."""
    def __init__(self, image, toc, stylesheet):
        self.image, self.toc, self.stylesheet = image, toc, stylesheet
        self.stack = []
        self.toc_depth = 0
        self.has_toc = False
        self.style = []

    def css(self, text):
        for kind, value in _css_references(text):
            (self.stylesheet if kind == 'stylesheet' else self.image)(value)

    def start(self, tag, attrs):
        tag = tag.rsplit('}', 1)[-1].rsplit(':', 1)[-1].lower()
        attrs = {key.rsplit('}', 1)[-1].lower(): value for key, value in attrs.items()}
        is_toc = tag == 'nav' and 'toc' in (attrs.get('epub:type') or attrs.get('type') or '').split()
        self.stack.append((tag, is_toc))
        if is_toc:
            self.toc_depth += 1
        if self.toc_depth and tag == 'ol':
            self.has_toc = True
        if self.toc_depth and tag == 'a' and attrs.get('href'):
            self.toc(attrs['href'])
        if attrs.get('style'):
            self.css(attrs['style'])
        if tag == 'link' and 'stylesheet' in attrs.get('rel', '').lower().split() and attrs.get('href'):
            self.stylesheet(attrs['href'])
        if tag in {'img', 'image'}:
            for attribute in ('src', 'href', 'xlink:href'):
                if attrs.get(attribute):
                    self.image(attrs[attribute])
        if tag in {'img', 'source'} and attrs.get('srcset'):
            for value in _srcset_urls(attrs['srcset']):
                self.image(value)
        if tag == 'video' and attrs.get('poster'):
            self.image(attrs['poster'])
        if tag == 'object' and attrs.get('type', '').startswith('image/') and attrs.get('data'):
            self.image(attrs['data'])
        if tag == 'input' and attrs.get('type', '').lower() == 'image' and attrs.get('src'):
            self.image(attrs['src'])

    def end(self, _tag):
        if self.stack:
            tag, is_toc = self.stack.pop()
            if is_toc:
                self.toc_depth -= 1
            if tag == 'style':
                self.css(''.join(self.style))
                self.style.clear()

    def data(self, value):
        if self.stack and self.stack[-1][0] == 'style':
            self.style.append(value)

    def close(self):
        return self.has_toc


def _xml(raw):
    root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    # Preserve ordinary EPUB2 external DOCTYPEs without opening their URLs.
    dtd = root.getroottree().docinfo.internalDTD
    if dtd is not None and any(dtd.iterentities()):
        raise EpubInputError(INVALID)
    return root


def _check(archive):
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES or sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_BYTES:
        raise EpubInputError(TOO_LARGE)
    members = {}
    for item in entries:
        name = item.filename
        if (name in members or '\\' in name or '\x00' in name or name.startswith('/')
                or posixpath.normpath(name).startswith('../') or posixpath.normpath(name) == '..'):
            raise EpubInputError(INVALID)
        if not item.is_dir():
            members[name] = item

    def read(name, limit=MAX_DOCUMENT_BYTES):
        item = members.get(name)
        if item is None:
            raise EpubInputError(MISSING_TEXT)
        if item.file_size > limit:
            raise EpubInputError(TOO_LARGE)
        with archive.open(item) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise EpubInputError(TOO_LARGE)
        return data

    def require(source, reference, message):
        path = _local_path(source, reference)
        if path is not None and path not in members:
            raise EpubInputError(message)
        return path

    container = _xml(read('META-INF/container.xml', MAX_METADATA_BYTES))
    package = selected_package_path(container)
    opf = _xml(read(package, MAX_METADATA_BYTES))
    manifest, spine = opf.find('{*}manifest'), opf.find('{*}spine')
    if manifest is None or spine is None:
        raise EpubInputError(INVALID)
    items, documents, styles, ncx, nav_documents = {}, set(), set(), {}, set()
    for item in manifest:
        uid, href, kind = item.get('id'), item.get('href'), item.get('media-type', '')
        if not uid or uid in items or not href:
            raise EpubInputError(INVALID)
        path = _local_path(package, href)
        items[uid] = (path, kind)
        if path not in members:
            if kind == 'application/oebps-page-map+xml' or missing_font_is_optional(opf, item):
                continue
            message = (MISSING_TEXT if kind in HTML_TYPES or kind == NCX_TYPE
                       else MISSING_IMAGE if kind.startswith('image/') else MISSING_RESOURCE)
            raise EpubInputError(message)
        if kind in HTML_TYPES or 'nav' in item.get('properties', '').split():
            require(package, href, MISSING_TEXT)
            if path is None:
                raise EpubInputError(INVALID)
            documents.add(path)
            if 'nav' in item.get('properties', '').split():
                nav_documents.add(path)
        elif kind.startswith('image/'):
            require(package, href, MISSING_IMAGE)
            if kind == 'image/svg+xml' and path is not None:
                documents.add(path)
        elif kind == 'text/css' and path in members:
            styles.add(path)
        elif kind == NCX_TYPE:
            # ebooklib reads every declared NCX file even when a valid nav
            # supersedes its links. Its file must still be present.
            require(package, href, MISSING_TEXT)
            if path is None:
                raise EpubInputError(INVALID)
            ncx[uid] = path
    if not len(spine):
        raise EpubInputError(MISSING_TEXT)
    for ref in spine:
        item = items.get(ref.get('idref'))
        if item is None or item[0] not in members:
            raise EpubInputError(MISSING_TEXT)

    declared_paths = {path for path, _kind in items.values()}

    def image(source, uri):
        path = require(source, uri, MISSING_IMAGE)
        if path is not None and path not in declared_paths:
            # A ZIP member that ebooklib drops is not a usable output resource.
            raise EpubInputError(UNDECLARED_RESOURCE)

    def stylesheet(source, uri):
        path = require(source, uri, MISSING_RESOURCE)
        if path is not None:
            styles.add(path)

    has_nav = False
    for name in documents:
        collector = _References(
            lambda uri, source=name: image(source, uri),
            lambda uri, source=name: require(source, uri, MISSING_TEXT),
            lambda uri, source=name: stylesheet(source, uri),
        )
        parser = etree.HTMLParser(target=collector, no_network=True)
        contains_toc = bool(etree.fromstring(read(name), parser))
        if any(error.level_name == 'FATAL' for error in parser.error_log):
            raise EpubInputError(INVALID)
        has_nav = (name in nav_documents and contains_toc) or has_nav
    checked_styles = set()
    while styles - checked_styles:
        name = next(iter(styles - checked_styles))
        checked_styles.add(name)
        for kind, uri in _css_references(read(name).decode('utf-8', errors='replace')):
            (stylesheet if kind == 'stylesheet' else image)(name, uri)
    if styles - declared_paths:
        raise EpubInputError(UNDECLARED_RESOURCE)
    # A usable EPUB3 nav deliberately supersedes a broken legacy NCX.
    if not has_nav and spine.get('toc'):
        name = ncx.get(spine.get('toc'))
        if name not in members:
            raise EpubInputError(MISSING_TEXT)
        root = _xml(read(name))
        for content in root.findall('.//{*}content'):
            if content.get('src'):
                require(name, content.get('src'), MISSING_TEXT)


def validate_epub_resources(stream):
    """Validate in place and restore the caller's stream offset on every exit."""
    position = stream.tell()
    try:
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            _check(archive)
    except EpubInputError:
        raise
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile, etree.LxmlError):
        raise EpubInputError(INVALID) from None
    finally:
        stream.seek(position)
