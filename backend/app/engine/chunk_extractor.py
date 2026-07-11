"""
从 XHTML 中提取块级元素为带稳定 locator 的 chunk，供 Manifest 与后续 Reduce 回写使用。

与 SemanticsTranslator 的块识别逻辑对齐：p, div, h1–h6, li, blockquote，
且仅叶子块（无同名子块）才作为独立 chunk。
"""

import re
import os
from dataclasses import dataclass
from typing import Any, List

from bs4 import BeautifulSoup, Tag


BLOCK_TAGS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]
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
    """Whether a block contains image media that should not be sent to the LLM."""
    if not _enabled("EPUB_SKIP_IMAGE_NOTE_CHUNKS", "1"):
        return False
    return bool(block.find(["img", "svg", "image"]))


def is_image_caption_block(block: Tag) -> bool:
    """Return True for a textual image caption/legend that should be translated."""
    if block.find(["img", "svg", "image"]):
        return False
    text = re.sub(r"\s+", " ", block.get_text(" ", strip=True) or "").strip()
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
    sentence_count = len(re.findall(r"[.!?](?:\s|$)", text))
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
    idx = same_siblings.index(tag) + 1
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


def _word_count(s: str) -> int:
    """简单按空白分词计数。"""
    return len(s.split()) if s.strip() else 0


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
    items: List[ChunkItem] = []
    stats = {
        "image_note_chunks_skipped": 0,
        "image_caption_chunks": 0,
        "reference_note_chunks_skipped": 0,
        "structured_note_chunks": 0,
    }
    seq = 0
    for block in blocks:
        if not _is_leaf_block(block):
            continue
        html = str(block)
        raw_text = block.get_text()
        if not raw_text.strip():
            continue
        seq += 1
        if should_skip_image_note_block(block):
            stats["image_note_chunks_skipped"] += 1
            continue
        if is_image_caption_block(block):
            stats["image_caption_chunks"] += 1
        if should_skip_reference_note_block(block):
            stats["reference_note_chunks_skipped"] += 1
            continue
        strategy = "text_nodes" if is_structured_note_block(block) else "html"
        if strategy == "text_nodes":
            stats["structured_note_chunks"] += 1
        locator = _build_locator(block, soup)
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
