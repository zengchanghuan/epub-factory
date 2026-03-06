import re

class CssSanitizer:
    """
    基于纯正则的 CSS 清洗器。
    绝不使用 BeautifulSoup 解析 HTML，因为 BS4 的 XML 解析器会破坏 SVG 的大小写敏感属性
    （如 preserveAspectRatio -> preserveaspectratio），导致封面图片缩放异常。
    """
    
    # 需要从 inline style 中移除的属性模式
    INLINE_PATTERNS = [
        re.compile(r'font-family\s*:[^;]+;?', re.IGNORECASE),
        re.compile(r'line-height\s*:\s*\d+px;?', re.IGNORECASE),
        re.compile(r'background-color\s*:[^;]+;?', re.IGNORECASE),
    ]

    def process(self, content: bytes, item_type: int) -> bytes:
        if item_type == 9:  # HTML 文件
            text = content.decode('utf-8', errors='ignore')
            
            # 用正则直接在 style="..." 属性值内部做替换
            def clean_style_attr(match):
                style_value = match.group(1)
                for pattern in self.INLINE_PATTERNS:
                    style_value = pattern.sub('', style_value)
                style_value = style_value.strip()
                # 如果 style 被洗空了，直接删除整个 style 属性
                if not style_value or style_value == ';':
                    return ''
                return f'style="{style_value}"'

            text = re.sub(r'style="([^"]*)"', clean_style_attr, text)
            
            return text.encode('utf-8')
            
        return content