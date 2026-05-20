"""
markdown_adapter
================
Converts a .md / .markdown file to (html_body, metadata) using mistune.

Supports YAML front-matter for structured metadata:

    ---
    title: 我的小说
    author: 张三
    language: zh
    ---

    # 第一章 ...

If no front-matter is present, the first <h1> heading is used as title
and language defaults to "zh".

Dependency: mistune>=3.0  (pip install "mistune>=3.0")
"""

import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_FRONT_MATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def md_to_html(input_path: Path) -> tuple[str, dict]:
    """
    Convert *input_path* (.md / .markdown) to an HTML body string and
    metadata dict.

    Returns
    -------
    html_body : str
        Inner HTML (no <html>/<body> wrapper).
    metadata  : dict
        Keys: title, author, language, identifier (all may be empty strings).

    Raises
    ------
    RuntimeError
        If mistune is not installed or parsing fails.
    """
    t0 = time.monotonic()
    try:
        import mistune
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Markdown conversion dependency missing: "
            "please install `mistune>=3.0` in current venv."
        ) from exc

    raw = input_path.read_text(encoding="utf-8", errors="replace")

    metadata, body_text = _parse_front_matter(raw)

    try:
        html_body = mistune.html(body_text)
    except Exception as exc:
        raise RuntimeError(f"mistune failed to parse {input_path.name}: {exc}") from exc

    if not metadata.get("title"):
        metadata["title"] = _extract_first_h1(html_body) or input_path.stem

    elapsed = (time.monotonic() - t0) * 1000
    logger.info("[markdown_adapter] converted %s in %.1f ms", input_path.name, elapsed)

    return html_body, metadata


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """
    Extract optional YAML front-matter from the top of *text*.
    Returns (metadata_dict, remaining_body).
    """
    meta = {"title": "", "author": "", "language": "zh", "identifier": ""}
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return meta, text

    body = text[m.end():]
    try:
        import yaml  # pyyaml is already in requirements.txt
        fm = yaml.safe_load(m.group(1)) or {}
        if isinstance(fm, dict):
            meta["title"] = str(fm.get("title", "") or "")
            meta["author"] = str(fm.get("author", "") or "")
            meta["language"] = str(fm.get("language", "zh") or "zh")
            meta["identifier"] = str(fm.get("identifier", "") or "")
    except Exception as exc:
        logger.debug("[markdown_adapter] front-matter parse error: %s", exc)

    return meta, body


def _extract_first_h1(html_body: str) -> str:
    """Pull the text of the first <h1> tag as a fallback title."""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html_body, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()
