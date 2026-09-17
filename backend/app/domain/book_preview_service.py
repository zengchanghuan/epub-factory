"""Read-only, sandboxed chapter preview; no scripts or remote resources."""
import base64
import html
import posixpath
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile
from bs4 import BeautifulSoup, Comment

_TAGS = set('p div h1 h2 h3 h4 h5 h6 span small b i em strong sub sup ul ol li br hr blockquote table thead tbody tr td th figcaption figure a img'.split())
_DROP = set('script style link object embed form input button textarea select iframe meta base svg math audio video'.split())
_STYLE = 'body{max-width:44em;margin:24px auto;padding:0 16px;color:#202124;background:white;font:18px/1.8 Georgia,serif;overflow-wrap:anywhere}img{max-width:100%;height:auto}table{max-width:100%;border-collapse:collapse}td,th{border:1px solid #ddd;padding:4px}blockquote{margin-left:1em}.epub-original{display:block;color:#666;font-size:.9em}.epub-translated{display:block;margin-bottom:1em}'


def _read(archive, name, limit):
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError('预览内容超过安全大小限制，请下载阅读')
    return archive.read(name)


def _local(base, href):
    parsed = urlsplit(href or '')
    if parsed.scheme or parsed.netloc:
        return None, ''
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parsed.path))) if parsed.path else base, unquote(parsed.fragment)


def _raster_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if data.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')): return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return 'image/webp'
    return None


def sanitize_chapter(archive, name, title):
    soup = BeautifulSoup(_read(archive, name, 2*1024*1024), 'html.parser')
    body = soup.find('body') or soup
    omitted = 0
    for svg in body.find_all('svg'):
        images = svg.find_all('image')
        if images:
            figure = soup.new_tag('figure')
            for image in images:
                raster = soup.new_tag('img')
                raster['src'] = image.get('href') or image.get('xlink:href') or ''
                raster['alt'] = title
                figure.append(raster)
            svg.replace_with(figure)
        else:
            svg.replace_with('[此矢量图请下载后查看]')
            omitted += 1
    for comment in body.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in list(body.find_all(True)):
        if tag.parent is None: continue
        if tag.name in _DROP: tag.decompose()
    image_budget = 8*1024*1024
    for tag in list(body.find_all(True)):
        if tag.name not in _TAGS:
            tag.unwrap()
            continue
        old = dict(tag.attrs)
        tag.attrs = {}
        if old.get('id'): tag['id'] = str(old['id'])[:200]
        classes = old.get('class') or []
        safe_classes = [c for c in classes if c in {'epub-original', 'epub-translated'}]
        if safe_classes: tag['class'] = safe_classes
        if tag.name == 'a':
            path, fragment = _local(name, old.get('href', ''))
            if path == name and fragment: tag['href'] = '#' + fragment
        elif tag.name == 'img':
            path, _ = _local(name, old.get('src', ''))
            data = None
            if path in archive.namelist() and archive.getinfo(path).file_size <= min(image_budget, 5*1024*1024):
                data = archive.read(path)
            media = _raster_type(data or b'')
            if not media:
                omitted += 1
                tag.replace_with('[此图片请下载后查看]')
            else:
                image_budget -= len(data)
                tag['src'] = 'data:' + media + ';base64,' + base64.b64encode(data).decode('ascii')
                tag['alt'] = str(old.get('alt') or '')[:300]
        elif tag.name in {'td', 'th'}:
            for attr in ('colspan', 'rowspan'):
                value = str(old.get(attr) or '')
                if value.isdigit() and 1 <= int(value) <= 100: tag[attr] = value
    document = ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
                '<title>' + html.escape(title) + '</title><style>' + _STYLE + '</style></head><body>'
                + body.decode_contents() + '</body></html>')
    return document, omitted


def build_book_preview(output_path, chapter_index=0):
    with zipfile.ZipFile(output_path) as archive:
        container = ET.fromstring(_read(archive, 'META-INF/container.xml', 256*1024))
        rootfile = container.find('.//{*}rootfile')
        if rootfile is None: raise ValueError('EPUB 缺少主目录文件')
        opf_name = rootfile.get('full-path', '')
        package = ET.fromstring(_read(archive, opf_name, 512*1024))
        items = {item.get('id'): item for item in package.findall('./{*}manifest/{*}item')}
        labels = {}
        for item in items.values():
            if item.get('media-type') == 'application/x-dtbncx+xml':
                ncx_name, _ = _local(opf_name, item.get('href', ''))
                ncx = ET.fromstring(_read(archive, ncx_name, 512*1024))
                for point in ncx.findall('.//{*}navPoint'):
                    content, label = point.find('./{*}content'), point.find('./{*}navLabel/{*}text')
                    if content is not None and label is not None:
                        path, _ = _local(ncx_name, content.get('src', ''))
                        labels.setdefault(path, ''.join(label.itertext()).strip())
        chapters = []
        for ref in package.findall('./{*}spine/{*}itemref'):
            item = items.get(ref.get('idref'))
            if item is None or item.get('media-type') not in {'application/xhtml+xml', 'text/html'} or 'nav' in item.get('properties', '').split():
                continue
            path, _ = _local(opf_name, item.get('href', ''))
            if path in archive.namelist():
                chapters.append({'index': len(chapters), 'title': labels.get(path) or f'第 {len(chapters)+1} 节', 'path': path})
        if not chapters or len(chapters) > 1000: raise ValueError('EPUB 没有可预览章节或章节过多')
        if chapter_index < 0 or chapter_index >= len(chapters): raise IndexError('章节不存在')
        current = chapters[chapter_index]
        document, omitted = sanitize_chapter(archive, current['path'], current['title'])
        return {'chapter_index': chapter_index, 'total_chapters': len(chapters),
                'chapters': [{k: c[k] for k in ('index', 'title')} for c in chapters],
                'html': document, 'images_omitted': omitted}
