"""
CjkNormalizer — 繁简转换 + 横竖排规范化

管线（仅 output_mode=simplified 时完整走 L1-L3）：
  L0: 编码探测/解码（encoding.py）
  L1: OpenCC 字形 + 内置词典（t2s / tw2s / hk2s）
  L2: 自维护两岸用词词典（general / tech / movie，Aho-Corasick）
  L3: 专有名词词典（proper_noun，Aho-Corasick）
  L4: DeepSeek 精校（见 llm_polish.py，外部调用）

本模块只负责 L0-L3，L4 由外部在整本处理完成后追加。
"""

import re
import logging
from typing import List, Optional

from opencc import OpenCC

from app.utils.encoding import decode_with_fallback
from .lexicon_matcher import LexiconMatcher, LexiconReport

logger = logging.getLogger("epub_factory.cjk")

# 繁体来源 → OpenCC 配置（选择「简体」时使用）
_OPENCC_SIMPLIFIED_PROFILES = {
    "auto": "t2s",
    "tw": "tw2s",
    "hk": "hk2s",
}

# 目标繁体版本 → OpenCC 配置（选择「繁体」时使用）
_OPENCC_TRADITIONAL_PROFILES = {
    "auto": "s2t",
    "tw": "s2tw",
    "hk": "s2hk",
}


class CjkNormalizer:
    def __init__(
        self,
        output_mode: str = "simplified",
        traditional_variant: str = "auto",
        lexicon_domains: Optional[List[str]] = None,
        enable_proper_noun: bool = True,
    ):
        """
        output_mode: 'traditional' | 'simplified' | 'keep'
        traditional_variant:
          output_mode == 'simplified' 时表示繁体来源：auto/tw/hk → t2s/tw2s/hk2s
          output_mode == 'traditional' 时表示目标繁体：auto/tw/hk → s2t/s2tw/s2hk
        lexicon_domains: L2 启用的领域列表，默认 ["general", "tech", "movie"]
        enable_proper_noun: 是否启用 L3 专有名词词典，默认 True
        """
        self.output_mode = output_mode
        self.traditional_variant = (traditional_variant or "auto").lower()
        if self.traditional_variant not in _OPENCC_SIMPLIFIED_PROFILES:
            self.traditional_variant = "auto"

        # L1: OpenCC
        if output_mode == "simplified":
            profile = _OPENCC_SIMPLIFIED_PROFILES[self.traditional_variant]
            self._text_converter = OpenCC(profile)
        elif output_mode == "traditional":
            profile = _OPENCC_TRADITIONAL_PROFILES[self.traditional_variant]
            self._text_converter = OpenCC(profile)
        else:
            self._text_converter = None

        # L2+L3: 词典匹配器（仅 simplified 模式下启用，其他模式不需要大陆习惯词）
        self._lexicon_matcher: Optional[LexiconMatcher] = None
        if output_mode == "simplified":
            domains = lexicon_domains if lexicon_domains is not None else ["general", "tech", "movie"]
            try:
                self._lexicon_matcher = LexiconMatcher(
                    domains=domains,
                    enable_proper_noun=enable_proper_noun,
                )
            except Exception as e:
                logger.warning("LexiconMatcher init failed, L2/L3 disabled: %s", e)

        # 累计命中报告（整本书所有 HTML item 的汇总）
        self._report: Optional[LexiconReport] = None

    def process(self, content: bytes, item_type: int) -> bytes:
        if not content:
            return content

        # html (9)
        if item_type == 9:
            text = decode_with_fallback(content)
            text = self._horizontalize_css(text)
            text = self._replace_vertical_punctuation(text)
            # L2+L3 先于 L1 运行：词典中记录的是原始繁体形式（如「軟體」「滑鼠」），
            # 必须在 OpenCC 字形转换之前匹配，否则 OpenCC 会把「軟體」→「软体」
            # 导致 L2 的「軟體→软件」条目无法命中。
            if self._lexicon_matcher:
                text, item_report = self._lexicon_matcher.process_html(text)
                self._merge_report(item_report)
            # L1: OpenCC（对剩余未被 L2/L3 替换的字符做字形转换）
            if self._text_converter:
                text = self._text_converter.convert(text)
            return text.encode("utf-8")

        # css (2)
        if item_type == 2:
            text = decode_with_fallback(content)
            text = self._horizontalize_css(text)
            return text.encode("utf-8")

        return content

    def get_report(self) -> Optional[LexiconReport]:
        """返回整本书汇总的词典命中报告，供 ConversionResult 使用。"""
        return self._report

    def _merge_report(self, item_report: LexiconReport) -> None:
        """将单个 HTML item 的报告合并到整本报告。"""
        if not item_report.total_replacements:
            return
        if self._report is None:
            self._report = LexiconReport(versions=item_report.versions.copy())
        else:
            self._report.versions.update(item_report.versions)

        self._report.total_replacements += item_report.total_replacements

        # 合并 hits：按 (layer, tw) 去重累加
        existing: dict[tuple, "LexiconHit"] = {
            (h.layer, h.tw): h for h in self._report.hits
        }
        for hit in item_report.hits:
            key = (hit.layer, hit.tw)
            if key in existing:
                existing[key].count += hit.count
            else:
                from .lexicon_matcher import LexiconHit
                new_hit = LexiconHit(
                    layer=hit.layer, tw=hit.tw, cn=hit.cn,
                    count=hit.count, domain=hit.domain
                )
                existing[key] = new_hit
                self._report.hits.append(new_hit)

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
            "\uFE41": "\u300C",  # ﹁ → 「
            "\uFE42": "\u300D",  # ﹂ → 」
            "\uFE43": "\u300E",  # ﹃ → 『
            "\uFE44": "\u300F",  # ﹄ → 』
            "\uFE35": "\uFF08",  # ︵ → （
            "\uFE36": "\uFF09",  # ︶ → ）
            "\uFE37": "\u3014",  # ︷ → 〔
            "\uFE38": "\u3015",  # ︸ → 〕
            "\uFE39": "\u3010",  # ︹ → 【
            "\uFE3A": "\u3011",  # ︺ → 】
            "\uFE3F": "\u3008",  # ︿ → 〈
            "\uFE40": "\u3009",  # ﹀ → 〉
            "\uFE3D": "\u300A",  # ︽ → 《
            "\uFE3E": "\u300B",  # ︾ → 》
            "\uFE10": "\uFF0C",  # ︐ → ，
            "\uFE11": "\u3001",  # ︑ → 、
            "\uFE12": "\u3002",  # ︒ → 。
            "\uFE13": "\uFF1A",  # ︓ → ：
            "\uFE14": "\uFF1B",  # ︔ → ；
            "\uFE15": "\uFF01",  # ︕ → ！
            "\uFE16": "\uFF1F",  # ︖ → ？
        }
        for key, value in mapping.items():
            content = content.replace(key, value)
        return content
