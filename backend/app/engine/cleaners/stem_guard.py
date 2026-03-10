"""
STEM 内容守卫（STEM Guard）

针对学术/技术类书籍的两大痛点：
1. 表格防溢出：为宽表格注入横向滚动容器，防止内容截断
2. MathML / 内联 SVG 公式保护：标记豁免域，使后续清洗器跳过这些节点

处理策略（纯正则，不过 BeautifulSoup DOM 以保护大小写敏感属性）：
- 检测含 <table> 的 HTML：若表格无 overflow 样式则外包 <div class="epub-table-wrap">
- 检测含 <math> 的 HTML：为 <math> 元素注入 display:block/inline 保护属性
- 检测含 <svg> 的 HTML：确保顶层 <svg> 有 overflow="visible" 属性（防裁切）
- 在首个 CSS 文件中注入表格滚动规则（幂等）
"""

import re

_CSS_MARKER = "/* EPUB-Factory: StemGuard */"

_TABLE_CSS = f"""{_CSS_MARKER}
.epub-table-wrap {{
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  max-width: 100%;
  display: block;
}}
table {{
  border-collapse: collapse;
  max-width: 100%;
}}
math, .MathJax, .math {{
  overflow-x: auto;
  display: inline-block;
  max-width: 100%;
}}
"""


class StemGuard:
    def __init__(self):
        self._css_injected = False
        self.stats = {"stem_protected": 0}

    def process(self, content: bytes, item_type: int) -> bytes:
        if item_type == 2:  # CSS
            return self._inject_css(content)
        if item_type == 9:  # HTML
            return self._guard_html(content)
        return content

    # ─── CSS 注入 ────────────────────────────────────────────────────────────

    def _inject_css(self, content: bytes) -> bytes:
        text = content.decode("utf-8", errors="ignore")
        if _CSS_MARKER in text or self._css_injected:
            return content
        self._css_injected = True
        return (text + "\n" + _TABLE_CSS).encode("utf-8")

    # ─── HTML 守卫 ────────────────────────────────────────────────────────────

    def _guard_html(self, content: bytes) -> bytes:
        text = content.decode("utf-8", errors="ignore")
        lower = text.lower()

        if "<table" in lower:
            text = self._wrap_tables(text)
        if "<math" in lower:
            text = self._protect_mathml(text)
        if "<svg" in lower:
            text = self._protect_svg(text)

        return text.encode("utf-8")

    def _wrap_tables(self, text: str) -> str:
        """为没有 overflow 样式的 <table> 外包滚动容器（避免双重包裹）"""
        def maybe_wrap(match: re.Match) -> str:
            full_table = match.group(0)
            # 已经被包裹过就跳过
            before = text[: match.start()]
            if before.rstrip().endswith('class="epub-table-wrap">'):
                return full_table
            self.stats["stem_protected"] += 1
            return f'<div class="epub-table-wrap">{full_table}</div>'

        return re.sub(
            r"<table\b[^>]*>[\s\S]*?</table>",
            maybe_wrap,
            text,
            flags=re.IGNORECASE,
        )

    def _protect_mathml(self, text: str) -> str:
        """为 <math> 元素注入 display 属性（若缺失）"""
        def add_display(match: re.Match) -> str:
            tag = match.group(0)
            if "display=" in tag.lower():
                return tag
            self.stats["stem_protected"] += 1
            # 在 > 前插入 display="inline"
            return re.sub(r"\s*/?>$", ' display="inline">', tag)

        return re.sub(r"<math\b[^>]*>", add_display, text, flags=re.IGNORECASE)

    def _protect_svg(self, text: str) -> str:
        """为顶层 <svg> 注入 overflow="visible"（若缺失），防止公式被裁切"""
        def add_overflow(match: re.Match) -> str:
            tag = match.group(0)
            if "overflow=" in tag.lower():
                return tag
            self.stats["stem_protected"] += 1
            return re.sub(r"\s*/?>$", ' overflow="visible">', tag)

        return re.sub(r"<svg\b[^>]*>", add_overflow, text, flags=re.IGNORECASE)
