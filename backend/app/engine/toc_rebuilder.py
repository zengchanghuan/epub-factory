"""
启发式目录重建器（Heuristic TOC Rebuilder）

扫描所有 XHTML 文档，提取章节标题（基于 h1-h6 标签和加粗居中段落），
重建 toc.ncx (EPUB 2) 和 nav.xhtml (EPUB 3) 目录。

运行在 Packager 写入前，直接操作 ebooklib 的 Book 对象。
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urldefrag

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
    COMMON_ZH_TITLES = {
        "cover": "封面",
        "about the author": "作者简介",
        "title page": "书名页",
        "copyright page": "版权页",
        "contents": "目录",
        "acknowledgements": "致谢",
        "acknowledgments": "致谢",
        "introduction": "导言",
        "notes": "注释",
        "bibliographical notes": "参考书目说明",
        "bibliographic notes": "参考书目说明",
        "footnotes": "脚注",
    }

    def rebuild(
        self,
        book: epub.EpubBook,
        *,
        original_book_title: str | None = None,
        translated_book_title: str | None = None,
        target_lang: str | None = None,
    ) -> epub.EpubBook:
        if self._has_existing_toc(book):
            preserved = self._count_toc_entries(book.toc)
            updated = self._sync_existing_toc_titles(
                book,
                original_book_title=original_book_title,
                translated_book_title=translated_book_title,
                target_lang=target_lang,
            )
            self.stats = {
                "toc_generated": 0,
                "toc_preserved": preserved,
                "toc_titles_updated": updated,
            }
            print(
                f"🔧 [TOC] Preserved {preserved} existing entries"
                f" and updated {updated} title(s)"
            )
            return book

        entries = self._extract_entries(book)
        self.stats = {"toc_generated": len(entries)}
        
        if not entries:
            print("⚠️ [TOC] No heading elements found, skipping rebuild")
            return book

        self._inject_anchors(book, entries)
        self._set_toc(book, entries)
        print(f"🔧 [TOC] Rebuilt with {len(entries)} entries")
        return book

    @staticmethod
    def _has_existing_toc(book: epub.EpubBook) -> bool:
        def has_link(items) -> bool:
            for item in items or []:
                if isinstance(item, tuple):
                    section, children = item
                    if getattr(section, "href", None) and getattr(section, "title", None):
                        return True
                    if has_link(children):
                        return True
                    continue
                if getattr(item, "href", None) and getattr(item, "title", None):
                    return True
            return False

        return has_link(getattr(book, "toc", None))

    @classmethod
    def _count_toc_entries(cls, items) -> int:
        total = 0
        for item in items or []:
            total += 1
            if isinstance(item, tuple):
                total += cls._count_toc_entries(item[1])
        return total

    @staticmethod
    def _normalize_title(text: str) -> str:
        text = re.sub(r"\s+", " ", str(text or "")).strip().lower()
        return text.replace("’", "'")

    @staticmethod
    def _normalize_href(href: str) -> tuple[str, str]:
        path, fragment = urldefrag(unquote(str(href or "")))
        path = path.replace("\\", "/").lstrip("./")
        if not path:
            return "", fragment
        path = PurePosixPath(path).as_posix()
        return path, fragment

    @staticmethod
    def _is_meaningful_heading(text: str) -> bool:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        return bool(text and len(text) <= 200 and not re.fullmatch(r"[\d\s._-]+", text))

    def _document_title_maps(self, book: epub.EpubBook):
        exact_headings: dict[tuple[str, str], str] = {}
        first_headings: dict[str, str] = {}
        cjk_found = False

        for item in book.get_items():
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            file_name = item.get_name()
            if not file_name:
                continue
            file_name, _ = self._normalize_href(file_name)
            soup = BeautifulSoup(item.get_content(), "html.parser")
            for heading in soup.find_all(list(self.TAG_LEVELS)):
                text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()
                if not self._is_meaningful_heading(text):
                    continue
                if re.search(r"[\u3400-\u9fff]", text):
                    cjk_found = True
                first_headings.setdefault(file_name, text)
                anchor_id = str(heading.get("id") or "").strip()
                if anchor_id:
                    exact_headings[(file_name, anchor_id)] = text

        return exact_headings, first_headings, cjk_found

    def _localized_existing_title(
        self,
        title: str,
        href: str,
        *,
        exact_headings: dict[tuple[str, str], str],
        first_headings: dict[str, str],
        href_file_counts: dict[str, int],
        target_is_chinese: bool,
        original_book_title: str | None,
        translated_book_title: str | None,
    ) -> str:
        normalized_title = self._normalize_title(title)
        file_name, fragment = self._normalize_href(href)

        if (
            translated_book_title
            and original_book_title
            and normalized_title == self._normalize_title(original_book_title)
        ):
            return translated_book_title.strip()

        if target_is_chinese:
            common = self.COMMON_ZH_TITLES.get(normalized_title)
            if common:
                return common
            page_match = re.fullmatch(r"page\s+(\d+)", normalized_title)
            if page_match:
                return f"第{page_match.group(1)}页"
            if "prefatory note" in normalized_title:
                return "伯克序言" if "burke" in normalized_title else "序言"

        exact = exact_headings.get((file_name, fragment)) if fragment else None
        if exact:
            return exact
        if href_file_counts.get(file_name, 0) == 1:
            heading = first_headings.get(file_name)
            if heading:
                return heading
        return title

    def _sync_existing_toc_titles(
        self,
        book: epub.EpubBook,
        *,
        original_book_title: str | None,
        translated_book_title: str | None,
        target_lang: str | None,
    ) -> int:
        exact_headings, first_headings, cjk_found = self._document_title_maps(book)
        if target_lang:
            target_is_chinese = target_lang.lower().startswith("zh")
        else:
            target_is_chinese = cjk_found or bool(
                re.search(r"[\u3400-\u9fff]", translated_book_title or "")
            )
        href_file_counts: dict[str, int] = {}

        def count_files(items) -> None:
            for item in items or []:
                node = item[0] if isinstance(item, tuple) else item
                file_name, _ = self._normalize_href(getattr(node, "href", ""))
                if file_name:
                    href_file_counts[file_name] = href_file_counts.get(file_name, 0) + 1
                if isinstance(item, tuple):
                    count_files(item[1])

        count_files(book.toc)
        changed = 0

        def update(items) -> None:
            nonlocal changed
            for item in items or []:
                node = item[0] if isinstance(item, tuple) else item
                old_title = str(getattr(node, "title", "") or "")
                new_title = self._localized_existing_title(
                    old_title,
                    getattr(node, "href", ""),
                    exact_headings=exact_headings,
                    first_headings=first_headings,
                    href_file_counts=href_file_counts,
                    target_is_chinese=target_is_chinese,
                    original_book_title=original_book_title,
                    translated_book_title=translated_book_title,
                )
                if new_title and new_title != old_title:
                    node.title = new_title
                    changed += 1
                if isinstance(item, tuple):
                    update(item[1])

        update(book.toc)
        return changed

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
