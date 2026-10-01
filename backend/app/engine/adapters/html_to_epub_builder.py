"""
html_to_epub_builder
====================
Shared utility: turns an HTML body string + metadata dict into a
minimal but valid EPUB 3 ZIP file.

Supported DOCX and Markdown adapters funnel through here so the EPUB
assembly logic lives in exactly one place. PDF remains disabled publicly.
"""

import html as _html_mod
import hashlib
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def build(html_body: str, metadata: dict, output_epub: Path, *, deterministic: bool = False) -> None:
    """
    Write a minimal EPUB 3 file to *output_epub*.

    Parameters
    ----------
    html_body:
        Raw HTML string for the body content.  May contain full <p>,
        <h1>…<h6>, <ul>, <ol> etc.  Must NOT contain <html>/<body>
        wrappers — those are added here.
    metadata:
        Dict with optional keys:
          title      (str)  — book title, default "Untitled"
          author     (str)  — dc:creator, default ""
          language   (str)  — BCP-47 tag, default "zh"
          identifier (str)  — unique book ID, auto-generated if absent
    output_epub:
        Destination path (parent dir must exist).
    deterministic:
        Opt-in for ephemeral translation normalization, not publication time.
        Uses stable fallback ID, modification marker and ZIP member timestamps.
        Existing ordinary-conversion callers retain their original behavior.
    """
    title = _html_mod.escape(metadata.get("title") or "Untitled")
    author = _html_mod.escape(metadata.get("author") or "")
    language = _html_mod.escape(metadata.get("language") or "zh", quote=True)
    stable_identity = hashlib.sha256(json.dumps(
        {"body": html_body, "metadata": metadata}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest() if deterministic else ""
    identifier = _html_mod.escape(metadata.get("identifier") or (
        "urn:sha256:" + stable_identity if deterministic else str(uuid.uuid4())), quote=True)
    modified = "1980-01-01T00:00:00Z" if deterministic else datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    chapter_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" lang="{language}">
  <head>
    <meta charset="utf-8"/>
    <title>{title}</title>
    <style>
      html, body {{
        writing-mode: horizontal-tb;
        line-height: 1.8;
        margin: 1rem;
        font-family: serif;
      }}
      h1, h2, h3, h4, h5, h6 {{ margin-top: 1.5em; margin-bottom: 0.5em; }}
      p {{ margin: 0.5em 0; text-indent: 2em; }}
    </style>
  </head>
  <body id="chapter-start">
{html_body}
  </body>
</html>
"""

    creator_elem = f"    <dc:creator>{author}</dc:creator>\n" if author else ""

    content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookId">{identifier}</dc:identifier>
    <dc:title>{title}</dc:title>
{creator_elem}    <dc:language>{language}</dc:language>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  </manifest>
  <spine page-progression-direction="ltr">
    <itemref idref="chapter1"/>
  </spine>
</package>
"""

    nav_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{language}" lang="{language}">
  <head><title>Contents</title></head>
  <body><nav epub:type="toc" id="toc"><h1>Contents</h1><ol>
    <li><a href="chapter1.xhtml#chapter-start">{title}</a></li>
  </ol></nav></body>
</html>
"""

    container_xml = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    output_epub.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_epub, "w") as zf:
        for name, content, compression in (
            ("mimetype", "application/epub+zip", zipfile.ZIP_STORED),
            ("META-INF/container.xml", container_xml, zipfile.ZIP_DEFLATED),
            ("OEBPS/content.opf", content_opf, zipfile.ZIP_DEFLATED),
            ("OEBPS/chapter1.xhtml", chapter_xhtml, zipfile.ZIP_DEFLATED),
            ("OEBPS/nav.xhtml", nav_xhtml, zipfile.ZIP_DEFLATED),
        ):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)) if deterministic else name
            zf.writestr(entry, content, compress_type=compression)
