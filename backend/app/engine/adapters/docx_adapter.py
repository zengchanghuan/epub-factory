"""
docx_adapter
============
Converts a .docx file to (html_body, metadata) using mammoth.

mammoth is purpose-built for DOCX → semantic HTML: it strips Word's
inline styles and outputs clean <h1>…<p>…<ul> markup, which is ideal
for feeding into ExtremeCompiler's CSS sanitizer and TOC rebuilder.

Dependency: mammoth  (pip install mammoth)
"""

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def docx_to_html(input_path: Path) -> tuple[str, dict]:
    """
    Convert *input_path* (.docx) to an HTML body string and metadata dict.

    Returns
    -------
    html_body : str
        Inner HTML (no <html>/<body> wrapper).
    metadata  : dict
        Keys: title, author, language, identifier (all may be empty strings).

    Raises
    ------
    RuntimeError
        If mammoth is not installed or conversion fails.
    """
    t0 = time.monotonic()
    try:
        import mammoth
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "DOCX conversion dependency missing: please install `mammoth` in current venv."
        ) from exc

    try:
        with open(input_path, "rb") as fh:
            result = mammoth.convert_to_html(fh)
    except Exception as exc:
        raise RuntimeError(f"mammoth failed to convert {input_path.name}: {exc}") from exc

    html_body = result.value or ""

    if result.messages:
        for msg in result.messages:
            logger.warning("[docx_adapter] mammoth warning: %s", msg)

    metadata = _extract_docx_metadata(input_path)

    elapsed = (time.monotonic() - t0) * 1000
    logger.info(
        "[docx_adapter] converted %s in %.1f ms  (warnings=%d)",
        input_path.name, elapsed, len(result.messages),
    )

    return html_body, metadata


def _extract_docx_metadata(input_path: Path) -> dict:
    """
    Pull core properties (title, author, language) from the DOCX ZIP.
    Falls back to empty strings gracefully so the caller always gets a dict.
    """
    import zipfile
    import xml.etree.ElementTree as ET

    meta = {"title": "", "author": "", "language": "zh", "identifier": ""}

    NS_CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    NS_DC = "http://purl.org/dc/elements/1.1/"
    NS_DCT = "http://purl.org/dc/terms/"

    try:
        with zipfile.ZipFile(input_path, "r") as zf:
            if "docProps/core.xml" in zf.namelist():
                raw = zf.read("docProps/core.xml")
                root = ET.fromstring(raw)
                title_el = root.find(f"{{{NS_DC}}}title")
                creator_el = root.find(f"{{{NS_DC}}}creator")
                lang_el = root.find(f"{{{NS_DCT}}}language")
                if title_el is not None and title_el.text:
                    meta["title"] = title_el.text.strip()
                if creator_el is not None and creator_el.text:
                    meta["author"] = creator_el.text.strip()
                if lang_el is not None and lang_el.text:
                    meta["language"] = lang_el.text.strip()
    except Exception as exc:
        logger.debug("[docx_adapter] could not read core properties: %s", exc)

    if not meta["title"]:
        meta["title"] = input_path.stem

    return meta
