import re
from opencc import OpenCC

from app.utils.encoding import decode_with_fallback

# 繁体来源 → OpenCC 配置（仅在选择「简体」时使用）
_OPENCC_SIMPLIFIED_PROFILES = {
    "auto": "t2s",   # 通用
    "tw": "tw2s",    # 台湾繁体
    "hk": "hk2s",    # 香港繁体
}


class CjkNormalizer:
    def __init__(self, output_mode: str = "simplified", traditional_variant: str = "auto"):
        # output_mode: 'traditional' | 'simplified' | 'keep'
        # traditional_variant: 'auto' | 'tw' | 'hk'，仅当 output_mode=='simplified' 时生效
        self.output_mode = output_mode
        self.traditional_variant = (traditional_variant or "auto").lower()
        if self.traditional_variant not in _OPENCC_SIMPLIFIED_PROFILES:
            self.traditional_variant = "auto"
        if output_mode == "simplified":
            profile = _OPENCC_SIMPLIFIED_PROFILES[self.traditional_variant]
            self._simplified_converter = OpenCC(profile)
        else:
            self._simplified_converter = None

    def process(self, content: bytes, item_type: int) -> bytes:
        if not content:
            return content  # None 或空 bytes 直接透传，上层不调用 set_content
        # html (9)
        if item_type == 9:
            text = decode_with_fallback(content)
            text = self._horizontalize_css(text)
            text = self._replace_vertical_punctuation(text)
            if self._simplified_converter:
                text = self._simplified_converter.convert(text)
            return text.encode("utf-8")

        # css (2)
        if item_type == 2:
            text = decode_with_fallback(content)
            text = self._horizontalize_css(text)
            return text.encode("utf-8")

        return content

    def _horizontalize_css(self, content: str) -> str:
        replacements = [
            (r"writing-mode\s*:\s*vertical-[lr]l\s*;?", "writing-mode: horizontal-tb;"),
            (r"-webkit-writing-mode\s*:\s*vertical-[lr]l\s*;?", "-webkit-writing-mode: horizontal-tb;"),
            (r"-epub-writing-mode\s*:\s*vertical-[lr]l\s*;?", "-epub-writing-mode: horizontal-tb;"),
            (r"text-orientation\s*:\s*upright\s*;?", "text-orientation: mixed;"),
            (r"direction\s*:\s*rtl\s*;?", "direction: ltr;"),
        ]
        for pattern, replacement in replacements:
            content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
        return content

    def _replace_vertical_punctuation(self, content: str) -> str:
        mapping = {
            "﹁": "「",
            "﹂": "」",
            "﹃": "『",
            "﹄": "』",
            "︵": "（",
            "︶": "）",
            "︷": "〔",
            "︸": "〕",
            "︹": "【",
            "︺": "】",
            "︿": "〈",
            "﹀": "〉",
            "︽": "《",
            "︾": "》",
            "\uFE10": "，",
            "\uFE11": "、",
            "\uFE12": "。",
            "\uFE13": "：",
            "\uFE14": "；",
            "\uFE15": "！",
            "\uFE16": "？",
        }
        for key, value in mapping.items():
            content = content.replace(key, value)
        return content
