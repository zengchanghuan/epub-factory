"""
从 XHTML 中提取块级元素为带稳定 locator 的 chunk，供 Manifest 与后续 Reduce 回写使用。

与 SemanticsTranslator 的块识别逻辑对齐：p, div, h1–h6, li, blockquote，
且仅叶子块（无同名子块）才作为独立 chunk。
"""

import re
from dataclasses import dataclass, field
from typing import List

from bs4 import BeautifulSoup, NavigableString, Tag


BLOCK_TAGS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]


@dataclass
class ChunkItem:
    chunk_id: str
    sequence: int
    locator: str
    html: str
    text: str
    word_count: int
    char_count: int


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


def extract_chunks(html_content: bytes, chapter_id: str) -> List[ChunkItem]:
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
    seq = 0
    for block in blocks:
        if not _is_leaf_block(block):
            continue
        html = str(block)
        raw_text = block.get_text()
        if not raw_text.strip():
            continue
        seq += 1
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
            )
        )
    return items
