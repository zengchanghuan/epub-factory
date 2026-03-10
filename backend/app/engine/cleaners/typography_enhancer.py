"""
排版增强器（Typography Enhancer）

负责修复两类常见的专业排版问题：
1. CJK 两端对齐与换行：注入 overflow-wrap/orphans/widows 规则，防止字间距撕裂
2. 智能标点修复：将连续省略号 ... → …，双短横 -- → —
"""

import re


# 只注入一次的标识注释
_MARKER = "/* EPUB-Factory: TypographyEnhancer */"

_TYPOGRAPHY_CSS = f"""{_MARKER}
p, li, dd, dt, blockquote {{
  orphans: 2;
  widows: 2;
  overflow-wrap: break-word;
  word-break: break-word;
}}
"""


class TypographyEnhancer:
    def __init__(self):
        self._css_injected = False
        self.stats = {"typography_fixed": 0}

    def process(self, content: bytes, item_type: int) -> bytes:
        if item_type == 2:  # CSS
            return self._enhance_css(content)
        if item_type == 9:  # HTML
            return self._enhance_html(content)
        return content

    def _enhance_css(self, content: bytes) -> bytes:
        text = content.decode("utf-8", errors="ignore")
        if _MARKER in text:
            return content
        # 已注入过一个文件就够了（避免重复注入到每个 CSS）
        if self._css_injected:
            return content
        self._css_injected = True
        return (text + "\n" + _TYPOGRAPHY_CSS).encode("utf-8")

    def _enhance_html(self, content: bytes) -> bytes:
        text = content.decode("utf-8", errors="ignore")
        text = self._fix_punctuation(text)
        return text.encode("utf-8")

    def _fix_punctuation(self, html: str) -> str:
        """在 HTML 文本节点中修复标点，跳过标签内部属性"""
        # 用正则只处理 > 和 < 之间的文本节点
        def fix_text_node(match):
            text = match.group(1)
            original = text
            # 三个及以上的点 → 省略号（保留已是省略号的情况）
            text = re.sub(r"\.{3,}", "…", text)
            # 连续两个短横（不在 URL 或属性边界） → 破折号
            text = re.sub(r"(?<![:/\-])\-{2}(?![\-/>])", "—", text)
            
            if text != original:
                self.stats["typography_fixed"] += 1
                
            return f">{text}<"

        return re.sub(r">([^<]+)<", fix_text_node, html)
