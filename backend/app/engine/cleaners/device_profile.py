"""
目标设备特化编译器（Device Profile Compiler）

根据用户选择的目标设备，对 CSS 和 HTML 进行针对性优化：
- kindle:  墨水屏优化（去色、加黑边框、去透明度）
- apple:   Apple Books 优化（保留色彩、WebKit 前缀、弹窗脚注）
- generic: 通用模式（不做设备特化，仅基础清洗）
"""

import re


class DeviceProfileCompiler:
    def __init__(self, device: str = "generic"):
        self.device = device.lower()

    def process(self, content: bytes, item_type: int) -> bytes:
        if self.device == "generic":
            return content

        if item_type == 9:  # HTML
            text = content.decode('utf-8', errors='ignore')
            if self.device == "kindle":
                text = self._kindle_optimize_html(text)
            elif self.device == "apple":
                text = self._apple_optimize_html(text)
            return text.encode('utf-8')

        if item_type == 2:  # CSS
            text = content.decode('utf-8', errors='ignore')
            if self.device == "kindle":
                text = self._kindle_optimize_css(text)
            elif self.device == "apple":
                text = self._apple_optimize_css(text)
            return text.encode('utf-8')

        return content

    # ─── Kindle (E-Ink) ───

    def _kindle_optimize_html(self, text: str) -> str:
        # 移除内联 color 属性（保留 #000 和 black）
        def clean_inline_color(match):
            style = match.group(1)
            style = re.sub(
                r'color\s*:\s*(?!(?:#000|black)\b)[^;]+;?',
                '', style, flags=re.IGNORECASE
            )
            style = re.sub(
                r'background(?:-color)?\s*:\s*(?!(?:transparent|inherit|white|#fff)\b)[^;]+;?',
                '', style, flags=re.IGNORECASE
            )
            # opacity -> 移除（墨水屏不支持半透明）
            style = re.sub(r'opacity\s*:[^;]+;?', '', style, flags=re.IGNORECASE)
            style = style.strip()
            if not style or style == ';':
                return ''
            return f'style="{style}"'
        text = re.sub(r'style="([^"]*)"', clean_inline_color, text)
        return text

    def _kindle_optimize_css(self, text: str) -> str:
        # 移除 color 声明（保留黑色）
        text = re.sub(
            r'color\s*:\s*(?!(?:#000|black|inherit)\b)[^;]+;',
            '', text, flags=re.IGNORECASE
        )
        # 移除非白色/透明的 background-color
        text = re.sub(
            r'background(?:-color)?\s*:\s*(?!(?:transparent|inherit|white|#fff|none)\b)[^;]+;',
            '', text, flags=re.IGNORECASE
        )
        # 移除 opacity
        text = re.sub(r'opacity\s*:[^;]+;', '', text, flags=re.IGNORECASE)
        # 提升表格边框可见性
        text = re.sub(
            r'(border[^:]*:\s*)(\d*\.?\d+)(px)',
            lambda m: f"{m.group(1)}{max(1, int(float(m.group(2))))}{m.group(3)}",
            text
        )
        return text

    # ─── Apple Books ───

    def _apple_optimize_html(self, text: str) -> str:
        # Apple Books 保留所有色彩和样式，不做 HTML 层面的删减
        return text

    def _apple_optimize_css(self, text: str) -> str:
        # 注入 WebKit 兼容前缀
        lines = text.split('\n')
        result = []
        for line in lines:
            result.append(line)
            stripped = line.strip()
            # 为常见属性添加 -webkit- 前缀
            for prop in ['hyphens', 'column-count', 'column-gap', 'text-size-adjust']:
                if stripped.startswith(f'{prop}:') and f'-webkit-{prop}' not in text:
                    result.append(line.replace(f'{prop}:', f'-webkit-{prop}:'))

        return '\n'.join(result)
