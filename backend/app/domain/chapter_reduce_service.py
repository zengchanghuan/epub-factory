"""
Reduce 回写：按 locator 将 chunk 译文回填到章节 XHTML，支持单语与双语模式。
"""

import re
from typing import List, Optional, Protocol, Tuple

from bs4 import BeautifulSoup, Tag

# 与 chunk_extractor 一致，仅用于定位
BLOCK_TAGS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]


class ChunkResultLike(Protocol):
    """任意具有 locator、translated_html、sequence、chunk_id 的对象（如 ChunkResult）。"""
    locator: str
    translated_html: str
    sequence: int
    chunk_id: str


def _parse_locator_segment(seg: str) -> Tuple[str, int]:
    """解析路径段 'p[2]' -> ('p', 2)，'div' -> ('div', 1)。"""
    seg = seg.strip()
    m = re.match(r"^([a-z0-9]+)\[(\d+)\]$", seg, re.I)
    if m:
        return (m.group(1).lower(), int(m.group(2)))
    return (seg.lower() if seg else "html", 1)


def _get_direct_children(parent: Tag, tag_name: str) -> List[Tag]:
    """获取 parent 的直接子节点中标签名为 tag_name 的列表（文档顺序）。"""
    return [c for c in parent.children if isinstance(c, Tag) and c.name == tag_name]


def get_node_by_locator(soup: BeautifulSoup, locator: str) -> Optional[Tag]:
    """
    根据 locator 路径定位到唯一节点。
    locator 形如 "/html/body/div/p[1]"，与 chunk_extractor._build_locator 生成格式一致。
    从 document 根开始，按路径段逐级取直接子节点。
    """
    if not locator or not locator.strip():
        return None
    parts = [p for p in locator.strip("/").split("/") if p]
    if not parts:
        return None
    current = soup  # document 根，与 Tag 一样有 .children
    for seg in parts:
        if current is None:
            return None
        tag_name, one_based_idx = _parse_locator_segment(seg)
        children = _get_direct_children(current, tag_name)
        if one_based_idx < 1 or one_based_idx > len(children):
            return None
        current = children[one_based_idx - 1]
    return current if isinstance(current, Tag) else None


def apply_chunk_results(
    html_content: bytes,
    chunk_results: List[ChunkResultLike],
    bilingual: bool,
) -> bytes:
    """
    将翻译好的 chunk 按 locator 回写到章节 HTML。

    :param html_content: 原始章节 XHTML 字节
    :param chunk_results: 按 sequence 排序的 chunk 结果，每项需有 locator、translated_html
    :param bilingual: True 时保留原文并在其后插入译文（并加 class epub-original / epub-translated）
    :return: 回写后的 XHTML 字节
    """
    text = html_content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    # 按 sequence 排序，避免乱序回写导致后续 locator 失效
    sorted_chunks = sorted(
        (c for c in chunk_results if c.locator and c.translated_html is not None),
        key=lambda c: (getattr(c, "sequence", 0), getattr(c, "chunk_id", "")),
    )
    for cr in sorted_chunks:
        node = get_node_by_locator(soup, cr.locator)
        if node is None:
            continue
        try:
            frag = BeautifulSoup(cr.translated_html, "html.parser")
            new_tag = frag.find(BLOCK_TAGS) or frag.find()
            if new_tag is None:
                continue
            if bilingual:
                # 原文保留并加 class，译文插在原文之后
                cls = node.get("class") or []
                if "epub-original" not in cls:
                    node["class"] = cls + ["epub-original"]
                new_tag["class"] = list(new_tag.get("class") or []) + ["epub-translated"]
                node.insert_after(new_tag)
            else:
                node.replace_with(new_tag)
        except Exception:
            continue
    return soup.encode(formatter="html", encoding="utf-8")
