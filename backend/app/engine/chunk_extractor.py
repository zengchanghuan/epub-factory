"""
从 XHTML 中提取块级元素为带稳定 locator 的 chunk，供 Manifest 与后续 Reduce 回写使用。

与 SemanticsTranslator 的块识别逻辑对齐：p, div, h1–h6, li, blockquote，
且仅叶子块（无同名子块）才作为独立 chunk。
"""

import re
import os
from dataclasses import dataclass
from typing import Any, List

from bs4 import BeautifulSoup, Tag, NavigableString, Comment


BLOCK_TAGS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]
MEDIA_TAGS = {"img", "svg", "image"}
NON_TEXT_TAGS = MEDIA_TAGS | {"script", "style"}
IMAGE_ANNOTATION_CLASS_RE = re.compile(
    r"(figcaption|caption|figure|photo|picture|image|diagram|illustration|illus|legend|credit)",
    re.I,
)
NOTE_CLASS_RE = re.compile(r"(footnotes?|footnoteg|endnotes?|rearnotes?|note[-_ ]?text)", re.I)
REFERENCE_NOTE_RE = re.compile(
    r"(https?://|www\.|doi\s*:|isbn\s*:|\b(?:vol|no|pp?|eds?)\.\s*\d|\b(?:19|20)\d{2}\b)",
    re.I,
)


@dataclass
class ChunkItem:
    chunk_id: str
    sequence: int
    locator: str
    html: str
    text: str
    word_count: int
    char_count: int
    translation_strategy: str = "html"


def _enabled(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).lower() not in {"0", "false", "no", "off"}


def _attr_text(tag: Tag) -> str:
    values: list[str] = []
    for key in ("class", "id", "role", "epub:type", "type"):
        value = tag.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    return " ".join(values)


def should_skip_image_note_block(block: Tag) -> bool:
    """Only media-only blocks are exempt; an image bullet must not hide prose."""
    if not _enabled("EPUB_SKIP_IMAGE_NOTE_CHUNKS", "1"):
        return False
    return bool(block.find(list(MEDIA_TAGS))) and not visible_text_outside_media(block).strip()


def is_external_text_node(node: NavigableString) -> bool:
    """Never send scripts, comments or any media subtree to a text translator."""
    return (
        isinstance(node, NavigableString) and not isinstance(node, Comment)
        and not any(getattr(parent, "name", "") in NON_TEXT_TAGS for parent in node.parents)
    )


def visible_text_outside_media(block: Tag, separator: str = "") -> str:
    return separator.join(str(node) for node in block.find_all(string=True) if is_external_text_node(node))


def media_subtrees(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    return [str(tag) for tag in soup.find_all(list(MEDIA_TAGS))
            if not tag.find_parent(list(MEDIA_TAGS))]


def is_image_caption_block(block: Tag) -> bool:
    """Return True for a textual image caption/legend that should be translated."""
    text = re.sub(r"\s+", " ", visible_text_outside_media(block, " ")).strip()
    if not text:
        return False

    attr_text = _attr_text(block)
    ancestors = [
        parent for parent in block.parents
        if isinstance(parent, Tag) and parent.name not in {"html", "body", "[document]"}
    ]
    ancestor_text = " ".join(_attr_text(parent) for parent in ancestors)
    combined = f"{attr_text} {ancestor_text}"
    return bool(IMAGE_ANNOTATION_CLASS_RE.search(combined))


def is_structured_note_block(block: Tag) -> bool:
    """Return True for a real footnote/endnote container, not a正文 reference marker."""
    attr_text = _attr_text(block)
    ancestors = [
        parent for parent in block.parents
        if isinstance(parent, Tag) and parent.name not in {"html", "body", "[document]"}
    ]
    combined = " ".join([attr_text, *(_attr_text(parent) for parent in ancestors)])
    return bool(NOTE_CLASS_RE.search(combined))


def should_skip_reference_note_block(block: Tag) -> bool:
    """Keep strongly structured citation-only notes as source text."""
    if not is_structured_note_block(block):
        return False
    text = re.sub(r"\s+", " ", block.get_text(" ", strip=True) or "").strip()
    if not text:
        return True
    signals = REFERENCE_NOTE_RE.findall(text)
    has_url_or_identifier = bool(re.search(r"https?://|www\.|doi\s*:|isbn\s*:", text, re.I))
    # Author initials and "et al." are citation abbreviations, not explanatory
    # sentences (e.g. J. A. Anguera et al., ... (2013), doi:10.1038/...).
    sentence_text = re.sub(r"\b[A-Z]\.\s*", "", text)
    sentence_text = re.sub(r"\bet\s+al\.", "et al", sentence_text, flags=re.I)
    sentence_count = len(re.findall(r"[.!?](?:\s|$)", sentence_text))
    return (
        (has_url_or_identifier and len(text) <= 300 and sentence_count <= 1)
        or (len(signals) >= 2 and sentence_count <= 1)
    )


def _xpath_segment(tag: Tag, parent: Tag | None) -> str:
    """给定父节点，生成该 tag 在兄弟中的位置段，如 p[2]。"""
    if parent is None:
        return tag.name or "html"
    same_siblings = [c for c in parent.children if isinstance(c, Tag) and c.name == tag.name]
    if len(same_siblings) <= 1:
        return f"{tag.name}[1]"
    # Tag equality compares markup, not identity: duplicate paragraphs must not
    # all resolve to the first sibling with the same content.
    idx = next(i for i, sibling in enumerate(same_siblings, 1) if sibling is tag)
    return f"{tag.name}[{idx}]"


def _build_locator(block: Tag, soup: BeautifulSoup) -> str:
    """从根到 block 的稳定 XPath 风格路径。"""
    path: List[str] = []
    current: Tag | None = block
    while current is not None and current.name is not None:
        if current.name == "[document]":
            break
        parent = current.parent if isinstance(current, Tag) else None
        seg = _xpath_segment(current, parent)
        path.append(seg)
        current = parent if isinstance(parent, Tag) else None
    path.reverse()
    return "/" + "/".join(path) if path else "/html/body"


def _is_leaf_block(block: Tag) -> bool:
    """块是否为叶子块（不含 p/div/h1–h6/li/blockquote 子节点）。"""
    return block.find(BLOCK_TAGS) is None


def _locator_index(soup: BeautifulSoup) -> dict[int, str]:
    """Index by object identity in one traversal, preserving locator syntax.

    Scanning every same-tag sibling for every paragraph is quadratic in long
    chapters. Tag objects themselves cannot be dict keys here: BeautifulSoup's
    equality/hash serialize markup and conflate duplicate paragraphs.
    """
    locators = {}
    stack = [(soup, '')]
    while stack:
        parent, path = stack.pop()
        counts = {}
        for child in parent.children:
            if not isinstance(child, Tag): continue
            counts[child.name] = counts.get(child.name, 0) + 1
            child_path = f'{path}/{child.name}[{counts[child.name]}]'
            locators[id(child)] = child_path
            stack.append((child, child_path))
    return locators


def _word_count(s: str) -> int:
    """简单按空白分词计数。"""
    return len(s.split()) if s.strip() else 0


def is_source_placeholder_document(content: bytes | str | BeautifulSoup) -> bool:
    """Recognize only our fixed missing-source explanation, never arbitrary prose.

    A generic ``translate=no`` (or the marker on its own) cannot exempt real
    source text from translation or quality checks.
    """
    if isinstance(content, BeautifulSoup):
        soup = content
    else:
        marker = (b"data-epub-factory-missing-document" if isinstance(content, bytes)
                  else "data-epub-factory-missing-document")
        if not content or marker not in content:
            return False
        raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        soup = BeautifulSoup(raw or "", "html.parser")
    body = soup.find("body")
    if not body or body.get("data-epub-factory-missing-document") != "true":
        return False
    text = re.sub(r"\s+", "", body.get_text())
    return text in {
        "原文件缺少本章节本页仅说明原文件缺失，不代表译文；其余可用章节继续处理。",
        "原文件缺少本章節本頁僅說明原文件缺失，不代表譯文；其餘可用章節繼續處理。",
    }


def extract_chunks_with_stats(html_content: bytes, chapter_id: str) -> tuple[List[ChunkItem], dict[str, Any]]:
    """
    从 HTML 中提取块级 chunk，返回带稳定 locator 的列表。

    :param html_content: 原始 XHTML 字节
    :param chapter_id: 章节标识，用于生成 chunk_id（如 chap_06）
    :return: 按文档顺序的 ChunkItem 列表
    """
    text = html_content.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(text, "html.parser")
    blocks = soup.find_all(BLOCK_TAGS)
    locators = _locator_index(soup)
    items: List[ChunkItem] = []
    stats = {
        "image_note_chunks_skipped": 0,
        "image_caption_chunks": 0,
        "reference_note_chunks_skipped": 0,
        "structured_note_chunks": 0,
    }
    if is_source_placeholder_document(soup):
        stats["source_placeholder_documents_skipped"] = 1
        return [], stats
    seq = 0
    for block in blocks:
        if not _is_leaf_block(block):
            continue
        html = str(block)
        # Retain the old sequence numbering even for SVG-internal text. Locators
        # and existing cache/chunk identifiers must not shift after this fix.
        if not block.get_text().strip():
            continue
        seq += 1
        if should_skip_image_note_block(block):
            stats["image_note_chunks_skipped"] += 1
            continue
        raw_text = visible_text_outside_media(block)
        if not raw_text.strip():
            continue
        if is_image_caption_block(block):
            stats["image_caption_chunks"] += 1
        if should_skip_reference_note_block(block):
            stats["reference_note_chunks_skipped"] += 1
            continue
        structured_note = is_structured_note_block(block)
        strategy = "text_nodes" if structured_note or block.find(list(MEDIA_TAGS)) else "html"
        if structured_note:
            stats["structured_note_chunks"] += 1
        locator = locators[id(block)]
        chunk_id = f"{chapter_id}_{seq:04d}"
        items.append(
            ChunkItem(
                chunk_id=chunk_id,
                sequence=seq,
                locator=locator,
                html=html,
                text=raw_text.strip(),
                word_count=_word_count(raw_text),
                char_count=len(raw_text),
                translation_strategy=strategy,
            )
        )
    return items, stats


def extract_chunks(html_content: bytes, chapter_id: str) -> List[ChunkItem]:
    """
    从 HTML 中提取块级 chunk，返回带稳定 locator 的列表。

    :param html_content: 原始 XHTML 字节
    :param chapter_id: 章节标识，用于生成 chunk_id（如 chap_06）
    :return: 按文档顺序的 ChunkItem 列表
    """
    chunks, _ = extract_chunks_with_stats(html_content, chapter_id)
    return chunks
