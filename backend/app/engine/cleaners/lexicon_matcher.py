"""
LexiconMatcher — 两岸用词词典匹配器（L2 通用词典 + L3 专有名词）

架构：
- 使用 Aho-Corasick 多模字符串匹配（pyahocorasick 库）
- 词典来源：backend/data/lexicon/*.yaml
- 支持热加载、多领域独立开关、命中报告

安全约束：
- 仅匹配 ≥2 字词条（避免单字误伤）
- 调用方负责「仅在文本节点替换」（不动 HTML 标签）
- 长词优先由 Aho-Corasick 自动保证（最长匹配模式）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("epub_factory.lexicon")

_LEXICON_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "lexicon"

# 领域文件映射（L2 通用词典，默认全开）
_L2_DOMAIN_FILES: Dict[str, str] = {
    "general": "general.yaml",
    "tech": "tech.yaml",
    "movie": "movie.yaml",
}

# L3 专有名词词典文件
_L3_FILE = "proper_noun.yaml"

# L4 风险词扫描文件（只读 tw 字段，不做替换）
_RISKY_FILE = "risky.yaml"


@dataclass
class LexiconHit:
    layer: str          # "L2" | "L3"
    tw: str             # 台/港原词
    cn: str             # 大陆替换词
    count: int = 0
    domain: str = ""


@dataclass
class LexiconReport:
    versions: Dict[str, str] = field(default_factory=dict)   # domain → version
    hits: List[LexiconHit] = field(default_factory=list)
    total_replacements: int = 0

    def to_dict(self) -> dict:
        return {
            "versions": self.versions,
            "hits": [
                {"layer": h.layer, "tw": h.tw, "cn": h.cn, "count": h.count, "domain": h.domain}
                for h in self.hits if h.count > 0
            ],
            "total_replacements": self.total_replacements,
        }


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        logger.warning("lexicon file not found: %s", path)
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_automaton(entries: list[dict]) -> Optional[object]:
    """从词条列表构建 Aho-Corasick 自动机。词条短于 2 字的跳过。"""
    try:
        import ahocorasick
    except ImportError:
        logger.warning("pyahocorasick not installed, L2/L3 lexicon matching disabled")
        return None

    A = ahocorasick.Automaton()
    for e in entries:
        tw = e.get("tw", "")
        cn = e.get("cn", "")
        if not tw or not cn or len(tw) < 2:
            continue
        if tw in A:
            continue
        A.add_word(tw, (tw, cn))
    if len(A) == 0:
        return None
    A.make_automaton()
    return A


def _replace_with_automaton(text: str, automaton, hit_counter: dict) -> str:
    """
    用 Aho-Corasick 自动机对文本做替换，采用贪婪最长匹配（从左到右，命中后跳过已匹配区间）。
    hit_counter: {tw: count} 用于统计。
    """
    if automaton is None or not text:
        return text

    # 收集所有命中位置 (end_idx, tw, cn)
    matches: list[tuple[int, int, str, str]] = []
    for end_idx, (tw, cn) in automaton.iter(text):
        start_idx = end_idx - len(tw) + 1
        matches.append((start_idx, end_idx, tw, cn))

    if not matches:
        return text

    # 按起始位置排序，贪婪选择（无重叠）
    matches.sort(key=lambda x: x[0])
    selected: list[tuple[int, int, str, str]] = []
    last_end = -1
    for start, end, tw, cn in matches:
        if start > last_end:
            selected.append((start, end, tw, cn))
            last_end = end

    # 构建替换结果
    result: list[str] = []
    cursor = 0
    for start, end, tw, cn in selected:
        result.append(text[cursor:start])
        result.append(cn)
        hit_counter[tw] = hit_counter.get(tw, 0) + 1
        cursor = end + 1
    result.append(text[cursor:])
    return "".join(result)


# HTML 文本节点正则（匹配标签之间的文本，不修改标签属性）
_TAG_RE = re.compile(r"(<[^>]+>)")


def replace_in_html(html_text: str, automaton, hit_counter: dict) -> str:
    """
    只在 HTML 文本节点中做替换，保留标签结构。
    """
    parts = _TAG_RE.split(html_text)
    out: list[str] = []
    for part in parts:
        if part.startswith("<"):
            out.append(part)        # 标签原样保留
        else:
            out.append(_replace_with_automaton(part, automaton, hit_counter))
    return "".join(out)


class LexiconMatcher:
    """
    供 CjkNormalizer 调用的完整 L2+L3 匹配器。

    用法：
        matcher = LexiconMatcher(domains=["general", "tech"], enable_proper_noun=True)
        new_html, report = matcher.process_html(html_text)
    """

    def __init__(
        self,
        domains: List[str] | None = None,
        enable_proper_noun: bool = True,
    ):
        self._domains = domains if domains is not None else list(_L2_DOMAIN_FILES.keys())
        self._enable_proper_noun = enable_proper_noun
        self._l2_automatons: Dict[str, Tuple[object, str]] = {}  # domain → (automaton, version)
        self._l3_automaton: Optional[object] = None
        self._l3_version: str = ""
        self._load()

    def _load(self) -> None:
        # 加载 L2 各领域
        for domain in self._domains:
            fname = _L2_DOMAIN_FILES.get(domain)
            if not fname:
                continue
            data = _load_yaml(_LEXICON_DIR / fname)
            version = data.get("version", "unknown")
            entries = data.get("entries", [])
            automaton = _build_automaton(entries)
            if automaton is not None:
                self._l2_automatons[domain] = (automaton, version)
                logger.info("L2 loaded domain=%s version=%s entries=%d", domain, version, len(entries))

        # 加载 L3 专有名词
        if self._enable_proper_noun:
            data = _load_yaml(_LEXICON_DIR / _L3_FILE)
            self._l3_version = data.get("version", "unknown")
            entries = data.get("entries", [])
            self._l3_automaton = _build_automaton(entries)
            if self._l3_automaton:
                logger.info("L3 loaded proper_noun version=%s entries=%d", self._l3_version, len(entries))

    def process_html(self, html_text: str) -> Tuple[str, LexiconReport]:
        """对单段 HTML 文本进行 L2+L3 替换，返回 (新文本, 报告)。"""
        report = LexiconReport()
        hit_counter: dict[str, tuple[str, str, str]] = {}  # tw → (cn, layer, domain)

        # L2：逐领域依次替换（顺序：general → tech → movie）
        text = html_text
        for domain in self._domains:
            if domain not in self._l2_automatons:
                continue
            automaton, version = self._l2_automatons[domain]
            report.versions[domain] = version
            domain_hits: dict[str, int] = {}
            text = replace_in_html(text, automaton, domain_hits)
            for tw, cnt in domain_hits.items():
                # 从自动机中查 cn（逆查）
                for _, (atw, acn) in automaton.iter(tw):
                    if atw == tw:
                        hit_counter[tw] = (acn, "L2", domain)
                        break
                report.hits.append(LexiconHit(layer="L2", tw=tw, cn=hit_counter.get(tw, ("?", "", ""))[0], count=cnt, domain=domain))
                report.total_replacements += cnt

        # L3：专有名词
        if self._enable_proper_noun and self._l3_automaton:
            report.versions["proper_noun"] = self._l3_version
            l3_hits: dict[str, int] = {}
            text = replace_in_html(text, self._l3_automaton, l3_hits)
            for tw, cnt in l3_hits.items():
                for _, (atw, acn) in self._l3_automaton.iter(tw):
                    if atw == tw:
                        report.hits.append(LexiconHit(layer="L3", tw=tw, cn=acn, count=cnt, domain="proper_noun"))
                        report.total_replacements += cnt
                        break

        # 合并命中为 top hits（按 count 降序）
        report.hits.sort(key=lambda h: h.count, reverse=True)
        return text, report

    @staticmethod
    def get_risky_words() -> List[str]:
        """返回 risky.yaml 中的风险词列表，供 L4 扫描用。"""
        data = _load_yaml(_LEXICON_DIR / _RISKY_FILE)
        return [e.get("tw", "") for e in data.get("entries", []) if e.get("tw")]
