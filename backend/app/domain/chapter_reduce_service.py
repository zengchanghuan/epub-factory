"""
Reduce 回写：按 locator 将 chunk 译文回填到章节 XHTML，支持单语与双语模式。
"""

import re
from typing import List, Optional, Protocol, Tuple

from bs4 import BeautifulSoup, Tag

# 与 chunk_extractor 一致，仅用于定位
BLOCK_TAGS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]

BILINGUAL_STYLE = """
/* EPUB Factory: bilingual reading layout */
body {
  font-size: 1em;
  line-height: 1.75;
  font-family: Georgia, "Noto Serif CJK SC", "Songti SC", serif;
}
.epub-original, .epub-translated {
  display: block;
  text-indent: 0;
  text-align: start;
  white-space: normal;
  overflow-wrap: break-word;
  word-break: normal;
}
.epub-original {
  font-size: 0.9em;
  line-height: 1.65;
  margin: 0.9em 0 0.35em;
}
.epub-translated {
  font-size: 1em;
  line-height: 1.8;
  margin: 0 0 1.2em;
}
.epub-original + br { display: none; }
h1 .epub-original, h2 .epub-original, h3 .epub-original,
h4 .epub-original, h5 .epub-original, h6 .epub-original {
  font-size: 0.72em;
  margin-top: 0.6em;
  text-align: inherit;
}
h1 .epub-translated, h2 .epub-translated, h3 .epub-translated,
h4 .epub-translated, h5 .epub-translated, h6 .epub-translated {
  line-height: 1.4;
  margin-bottom: 0.8em;
  text-align: inherit;
}
"""


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


def _index_nodes_by_locator(soup: BeautifulSoup) -> dict[str, Tag]:
    """Index the original DOM once, counting siblings by tag and identity.

    Resolving each paragraph by scanning its siblings is quadratic in long
    chapters. Store the actual nodes before rewriting any contents so later
    locators cannot accidentally address markup introduced by a translation.
    """
    nodes: dict[str, Tag] = {}
    stack = [(soup, "")]
    while stack:
        parent, path = stack.pop()
        counts: dict[str, int] = {}
        for child in parent.children:
            if not isinstance(child, Tag):
                continue
            counts[child.name] = counts.get(child.name, 0) + 1
            child_path = f"{path}/{child.name}[{counts[child.name]}]"
            nodes[child_path] = child
            stack.append((child, child_path))
    return nodes


def _canonical_locator(locator: str) -> str | None:
    """Keep the legacy locator rules, including omitted [1] and tag case."""
    if not locator or not locator.strip():
        return None
    parts = [part for part in locator.strip("/").split("/") if part]
    if not parts:
        return None
    segments = [_parse_locator_segment(part) for part in parts]
    if any(index < 1 for _, index in segments):
        return None
    return "".join(f"/{tag}[{index}]" for tag, index in segments)


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
    # Apply in the existing deterministic order, resolving all original nodes
    # before any replacement instead of rescanning siblings for each chunk.
    sorted_chunks = sorted(
        (c for c in chunk_results if c.locator and c.translated_html is not None),
        key=lambda c: (getattr(c, "sequence", 0), getattr(c, "chunk_id", "")),
    )
    node_index = _index_nodes_by_locator(soup) if sorted_chunks else {}
    targets = [(cr, node_index.get(_canonical_locator(cr.locator))) for cr in sorted_chunks]
    for cr, node in targets:
        if node is None:
            continue
        try:
            frag = BeautifulSoup(cr.translated_html, "html.parser")
            
            # Check if translation is wrapped in an identical outer tag (legacy cache behavior)
            new_tag = frag.find(BLOCK_TAGS) or frag.find()
            if new_tag and new_tag.name == node.name:
                translated_contents = list(new_tag.contents)
            else:
                translated_contents = list(frag.contents)

            if bilingual:
                # Native safe bilingual injection
                original_span = soup.new_tag("span", attrs={"class": "epub-original"})
                original_span.extend(list(node.contents))
                
                translated_span = soup.new_tag("span", attrs={"class": "epub-translated"})
                translated_span.extend(translated_contents)
                # Inline note/page anchors exist in both model output and source.
                # Keep their original targets, without emitting duplicate XML IDs.
                original_ids = {tag["id"] for tag in original_span.find_all(id=True)}
                for tag in translated_span.find_all(id=True):
                    if tag["id"] in original_ids:
                        del tag["id"]
                
                node.clear()
                node.append(original_span)
                node.append(soup.new_tag("br"))
                node.append(translated_span)
            else:
                node.clear()
                node.extend(translated_contents)
        except Exception as e:
            print(f"Error applying chunk {cr.chunk_id}: {e}")
            continue
    return soup.encode(formatter="html", encoding="utf-8")
