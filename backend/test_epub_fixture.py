"""Small valid EPUB bytes for offline API tests; no customer content or model calls."""
import io
import zipfile


def minimal_epub_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
        archive.writestr('EPUB/package.opf', '''<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">offline-api-fixture</dc:identifier><dc:title>Offline fixture</dc:title><dc:language>en</dc:language><meta property="dcterms:modified">2026-09-18T00:00:00Z</meta></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="chapter"/></spine></package>''')
        archive.writestr('EPUB/chapter.xhtml', '''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head><body><h1 id="start">Chapter</h1><p>A short original sentence for an offline test.</p></body></html>''')
        archive.writestr('EPUB/nav.xhtml', '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="chapter.xhtml#start">Chapter</a></li></ol></nav></body></html>''')
    return stream.getvalue()
