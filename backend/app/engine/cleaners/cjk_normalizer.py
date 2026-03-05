import re
from opencc import OpenCC

class CjkNormalizer:
    def __init__(self, output_mode: str = "simplified"):
        # output_mode: 'traditional' | 'simplified' | 'keep'
        self.output_mode = output_mode
        if output_mode == "simplified":
            self._simplified_converter = OpenCC("t2s")
        else:
            self._simplified_converter = None

    def process(self, content: bytes, item_type: int) -> bytes:
        # html (9)
        if item_type == 9:
            text = content.decode('utf-8', errors='ignore')
            text = self._horizontalize_css(text)
            text = self._replace_vertical_punctuation(text)
            if self._simplified_converter:
                text = self._simplified_converter.convert(text)
            return text.encode('utf-8')
        
        # css (2)
        if item_type == 2:
            text = content.decode('utf-8', errors='ignore')
            text = self._horizontalize_css(text)
            return text.encode('utf-8')

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