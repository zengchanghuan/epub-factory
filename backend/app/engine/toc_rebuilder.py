"""
启发式目录重建器（Heuristic TOC Rebuilder）

扫描所有 XHTML 文档，提取章节标题（基于 h1-h6 标签和加粗居中段落），
重建 toc.ncx (EPUB 2) 和 nav.xhtml (EPUB 3) 目录。

运行在 Packager 写入前，直接操作 ebooklib 的 Book 对象。
"""

import re
from dataclasses import dataclass
from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub


@dataclass
class TocEntry:
    title: str
    href: str
    anchor_id: str
    level: int  # 1=h1, 2=h2, etc.


class TocRebuilder:
    """在 compiler pipeline 之外运行，直接操作 Book 对象"""

    TAG_LEVELS = {'h1': 1, 'h2': 2, 'h3': 3, 'h4': 4, 'h5': 5, 'h6': 6}

    def rebuild(self, book: epub.EpubBook) -> epub.EpubBook:
        entries = self._extract_entries(book)
        if not entries:
            print("⚠️ [TOC] No heading elements found, skipping rebuild")
            return book

        self._inject_anchors(book, entries)
        self._set_toc(book, entries)
        print(f"🔧 [TOC] Rebuilt with {len(entries)} entries")
        return book

    def _extract_entries(self, book: epub.EpubBook) -> list[TocEntry]:
        entries = []
        anchor_counter = 0

        for item in book.get_items():
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue

            file_name = item.get_name()
            base_name = file_name.rsplit('/', 1)[-1].lower()
            # 排除导航文件、NCX 和推广/附属页面
            if 'nav' in base_name or base_name.endswith('.ncx') or 'next-reads' in base_name:
                continue

            content = item.get_content()
            soup = BeautifulSoup(content, 'html.parser')

            # 策略 1: 从 h1-h6 标签抓取
            for tag in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
                text = tag.get_text(strip=True)
                if not text or len(text) > 200:
                    continue

                level = self.TAG_LEVELS[tag.name]
                existing_id = tag.get('id')

                if existing_id:
                    anchor_id = existing_id
                else:
                    anchor_id = f"toc_anchor_{anchor_counter}"
                    anchor_counter += 1

                entries.append(TocEntry(
                    title=text,
                    href=file_name,
                    anchor_id=anchor_id,
                    level=level,
                ))

            # 策略 2: 识别加粗居中的段落（可能是伪标题）
            for p_tag in soup.find_all('p'):
                style = p_tag.get('style', '')
                is_centered = 'text-align' in style and 'center' in style
                is_bold = (
                    ('font-weight' in style and 'bold' in style) or
                    p_tag.find('b') or p_tag.find('strong')
                )
                text = p_tag.get_text(strip=True)

                if is_centered and is_bold and text and 10 < len(text) < 100:
                    # 避免和已抓取的 heading 重复
                    if any(e.title == text and e.href == file_name for e in entries):
                        continue
                    anchor_id = f"toc_anchor_{anchor_counter}"
                    anchor_counter += 1
                    entries.append(TocEntry(
                        title=text,
                        href=file_name,
                        anchor_id=anchor_id,
                        level=2,
                    ))

        return entries

    def _inject_anchors(self, book: epub.EpubBook, entries: list[TocEntry]):
        """为没有 id 的标题注入锚点（纯字符串替换，不使用 BeautifulSoup 避免破坏 XHTML）"""
        anchors_by_file: dict[str, list[TocEntry]] = {}
        for entry in entries:
            anchors_by_file.setdefault(entry.href, []).append(entry)

        for item in book.get_items():
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            if item.get_name() not in anchors_by_file:
                continue

            content = item.get_content().decode('utf-8', errors='ignore')
            modified = False

            for entry in anchors_by_file[item.get_name()]:
                if f'id="{entry.anchor_id}"' in content:
                    continue
                # 跳过已有原始 id 的条目
                if not entry.anchor_id.startswith('toc_anchor_'):
                    continue

                escaped = re.escape(entry.title[:40])
                # 匹配不含 id= 的 heading 开标签，允许标签与文本之间有内嵌子标签
                pattern = rf'(<h[1-6]\b)(?![^>]*\bid=)([^>]*>)((?:(?!</h).)*?{escaped})'
                match = re.search(pattern, content)
                if match:
                    replacement = f'{match.group(1)} id="{entry.anchor_id}"{match.group(2)}{match.group(3)}'
                    content = content[:match.start()] + replacement + content[match.end():]
                    modified = True

            if modified:
                item.set_content(content.encode('utf-8'))

    def _set_toc(self, book: epub.EpubBook, entries: list[TocEntry]):
        """设置 ebooklib 的 TOC 结构（更新已有的，不重复添加）"""
        toc_items = []
        for entry in entries:
            link = epub.Link(
                f"{entry.href}#{entry.anchor_id}",
                entry.title,
                entry.anchor_id
            )
            toc_items.append(link)

        book.toc = toc_items
