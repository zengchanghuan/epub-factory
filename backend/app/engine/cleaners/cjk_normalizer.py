import re
from opencc import OpenCC

from app.utils.encoding import decode_with_fallback

# 繁体来源 → OpenCC 配置（选择「简体」时使用）
_OPENCC_SIMPLIFIED_PROFILES = {
    "auto": "t2s",   # 通用
    "tw": "tw2s",    # 台湾繁体
    "hk": "hk2s",    # 香港繁体
}

# 目标繁体版本 → OpenCC 配置（选择「繁体」时使用）
_OPENCC_TRADITIONAL_PROFILES = {
    "auto": "s2t",   # 通用繁体
    "tw": "s2tw",    # 台湾正体
    "hk": "s2hk",    # 港澳繁体
}


class CjkNormalizer:
    def __init__(self, output_mode: str = "simplified", traditional_variant: str = "auto"):
        # output_mode: 'traditional' | 'simplified' | 'keep'
        # traditional_variant:
        #   output_mode == 'simplified' 时表示繁体来源：auto/tw/hk → t2s/tw2s/hk2s
        #   output_mode == 'traditional' 时表示目标繁体：auto/tw/hk → s2t/s2tw/s2hk
        self.output_mode = output_mode
        self.traditional_variant = (traditional_variant or "auto").lower()
        if self.traditional_variant not in _OPENCC_SIMPLIFIED_PROFILES:
            self.traditional_variant = "auto"
        if output_mode == "simplified":
            profile = _OPENCC_SIMPLIFIED_PROFILES[self.traditional_variant]
            self._text_converter = OpenCC(profile)
        elif output_mode == "traditional":
            profile = _OPENCC_TRADITIONAL_PROFILES[self.traditional_variant]
            self._text_converter = OpenCC(profile)
        else:
            self._text_converter = None

    def process(self, content: bytes, item_type: int) -> bytes:
        if not content:
            return content  # None 或空 bytes 直接透传，上层不调用 set_content
        # html (9)
        if item_type == 9:
            text = decode_with_fallback(content)
            text = self._horizontalize_css(text)
            text = self._replace_vertical_punctuation(text)
            if self._text_converter:
                text = self._text_converter.convert(text)
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
