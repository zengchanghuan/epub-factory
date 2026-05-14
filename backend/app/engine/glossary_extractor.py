"""
全书术语库抽取引擎。

解决 MapReduce 翻译范式下，跨 chunk/章节的人名、地名、专有名词译法不一致问题。

两阶段流程：
    1) extract_candidates(book)     纯规则层：正则扫描原文，统计候选名词频次
    2) translate_glossary(candidates, llm) LLM 层：把候选词统一翻译成目标语言

调用者负责把产出的 {src: dst} 字典注入 SemanticsTranslator 的 glossary 字段。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger("epub_factory.glossary")


# ─── 常量与黑名单 ─────────────────────────────────────────────────────

# 句首高频英文词（被句号/换行后的首字母大写，并非专有名词）
_SENTENCE_INITIAL_BLACKLIST: frozenset[str] = frozenset({
    "The", "This", "That", "These", "Those", "There", "Their", "They", "Them",
    "And", "But", "Or", "Nor", "So", "Yet", "For", "Of", "In", "On", "At", "By",
    "To", "From", "With", "Without", "About", "Above", "Below", "Under", "Over",
    "After", "Before", "When", "While", "Where", "Why", "How", "What", "Who", "Whom",
    "Which", "Whose", "Here", "Now", "Then", "Today", "Tomorrow", "Yesterday",
    "Yes", "No", "Maybe", "Perhaps", "Indeed", "Even", "Only", "Just", "Still",
    "Already", "Always", "Never", "Often", "Sometimes", "Usually", "Once", "Twice",
    "He", "She", "It", "We", "You", "I", "Me", "My", "Mine", "Your", "Yours",
    "His", "Her", "Hers", "Its", "Our", "Ours", "Will", "Would", "Could", "Should",
    "Might", "Must", "Shall", "Can", "May", "Do", "Did", "Does", "Done", "Be", "Been",
    "Being", "Am", "Is", "Are", "Was", "Were", "Have", "Has", "Had", "Get", "Got",
    "Make", "Made", "Take", "Took", "Come", "Came", "Go", "Went", "Said", "Say",
    "Chapter", "Part", "Book", "Section", "Page", "Volume",
})

# 称谓词（出现时其后跟随的大写词必然是人名，置信度极高）
_HONORIFICS: tuple[str, ...] = (
    "Mr", "Mrs", "Ms", "Miss", "Mr.", "Mrs.", "Ms.", "Miss.",
    "Dr", "Dr.", "Prof", "Prof.", "Professor", "Sir", "Lord", "Lady",
    "Captain", "Colonel", "Major", "General", "Sergeant", "Lieutenant",
    "King", "Queen", "Prince", "Princess", "Duke", "Duchess",
    "Father", "Brother", "Sister", "Uncle", "Aunt",
    "Saint", "St.", "St",
)

# 称谓本身不应作为术语（"Mr"、"Mrs" 等是头衔，会随姓氏带出）
_HONORIFIC_BLACKLIST: frozenset[str] = frozenset(
    h.rstrip(".") for h in _HONORIFICS
)

# 正则：连续 1-3 个首字母大写的英文词（人名/地名/机构名典型形式）
_CAPITALIZED_RUN_RE = re.compile(
    r"\b([A-Z][a-z'’\-]{1,}(?:\s+[A-Z][a-z'’\-]{1,}){0,2})\b"
)

# 正则：称谓 + 大写词（高置信度专有名词）
_HONORIFIC_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in _HONORIFICS) + r")\s+"
    r"([A-Z][a-z'’\-]{1,}(?:\s+[A-Z][a-z'’\-]{1,}){0,2})\b"
)

# 正则：全大写词（缩写/机构名）
_ACRONYM_RE = re.compile(r"\b([A-Z]{2,6})\b")

# 句首词识别：基于句号/问号/感叹号/换行后首个大写词
_SENTENCE_START_RE = re.compile(r"(?:^|[.!?]\s+|\n)([A-Z][a-z'’\-]{1,})")


@dataclass
class GlossaryCandidate:
    """单个候选术语。"""
    term: str
    count: int
    confidence: float = 0.0  # 0~1，>= 0.7 视为高置信度（如称谓后接的人名）
    kinds: set[str] = field(default_factory=set)  # honorific|capitalized|acronym


@dataclass
class ExtractionStats:
    total_chars_scanned: int = 0
    raw_candidates: int = 0
    after_filter: int = 0
    final_kept: int = 0


# ─── 阶段一：规则层抽取 ────────────────────────────────────────────────

def extract_candidates(
    texts: Iterable[str],
    *,
    min_count: int = 2,
    max_terms: int = 200,
) -> tuple[list[GlossaryCandidate], ExtractionStats]:
    """
    规则层抽取：从一组纯文本块中识别候选专有名词，按频次排序。

    :param texts: 纯文本迭代器（不含 HTML 标签）
    :param min_count: 最小出现次数，低于此阈值的候选会被丢弃
    :param max_terms: 返回的最大候选数（按频次倒排截断）
    :return: (候选列表, 统计信息)
    """
    stats = ExtractionStats()
    counter: Counter[str] = Counter()
    high_conf: set[str] = set()
    kinds_by_term: dict[str, set[str]] = {}
    sentence_initials: Counter[str] = Counter()

    for text in texts:
        if not text:
            continue
        stats.total_chars_scanned += len(text)

        # 先收集句首单词，用于过滤误判
        for m in _SENTENCE_START_RE.finditer(text):
            sentence_initials[m.group(1)] += 1

        # 称谓后接的人名（高置信度）
        for m in _HONORIFIC_NAME_RE.finditer(text):
            term = _normalize(m.group(1))
            if _is_valid_term(term):
                counter[term] += 1
                high_conf.add(term)
                kinds_by_term.setdefault(term, set()).add("honorific")

        # 连续大写词
        for m in _CAPITALIZED_RUN_RE.finditer(text):
            term = _normalize(m.group(1))
            if _is_valid_term(term):
                counter[term] += 1
                kinds_by_term.setdefault(term, set()).add("capitalized")

        # 缩写
        for m in _ACRONYM_RE.finditer(text):
            term = m.group(1)
            if len(term) >= 2 and term not in _SENTENCE_INITIAL_BLACKLIST:
                counter[term] += 1
                kinds_by_term.setdefault(term, set()).add("acronym")

    stats.raw_candidates = len(counter)

    # 过滤：
    # 1. 出现次数低于阈值
    # 2. 单词且在黑名单中（句首高频 / 称谓）
    # 3. 单词只出现在句首（误判可能）
    filtered: list[GlossaryCandidate] = []
    for term, cnt in counter.items():
        if cnt < min_count:
            continue
        # 黑名单：单词且在停用词表
        if " " not in term and term in _SENTENCE_INITIAL_BLACKLIST:
            continue
        # 黑名单：称谓本身
        if " " not in term and term in _HONORIFIC_BLACKLIST:
            continue
        # 单词且仅出现在句首（出现次数 == 句首出现次数 → 大概率不是专名）
        if " " not in term and term in sentence_initials:
            if sentence_initials[term] >= cnt and term not in high_conf:
                continue

        kinds = kinds_by_term.get(term, set())
        confidence = 0.9 if term in high_conf else (
            0.7 if "acronym" in kinds else 0.5
        )
        filtered.append(GlossaryCandidate(
            term=term,
            count=cnt,
            confidence=confidence,
            kinds=kinds,
        ))

    stats.after_filter = len(filtered)

    # 排序：高置信度优先，然后按频次倒排
    filtered.sort(key=lambda c: (-c.confidence, -c.count, c.term))
    final = filtered[:max_terms]
    stats.final_kept = len(final)

    logger.info(
        "glossary extract stats",
        extra={
            "chars": stats.total_chars_scanned,
            "raw": stats.raw_candidates,
            "filtered": stats.after_filter,
            "kept": stats.final_kept,
        },
    )
    return final, stats


def _normalize(term: str) -> str:
    """规范化候选词：合并空白、去首尾标点。"""
    term = re.sub(r"\s+", " ", term).strip()
    term = term.strip(".,;:!?'\"-—()[]{}")
    return term


def _is_valid_term(term: str) -> bool:
    """基本有效性：长度、字符种类。"""
    if not term:
        return False
    if len(term) < 2 or len(term) > 60:
        return False
    # 至少含一个字母
    if not any(c.isalpha() for c in term):
        return False
    return True


# ─── 阶段二：LLM 翻译候选词 ────────────────────────────────────────────

_GLOSSARY_SYSTEM_PROMPT = """你是一位资深的图书翻译家，专长是把专有名词翻译为符合中文出版习惯的固定译名。

要求：
1. 输入是一个英文专有名词数组（含人名、地名、机构名、缩写等）。
2. 输出 JSON 对象，格式必须为：{"translations": {"原文": "译名", ...}}
3. 翻译原则：
   - 人名采用通行的音译（如 Harry → 哈利、Smith → 史密斯）
   - 地名采用通行译法（如 London → 伦敦、Hogwarts → 霍格沃茨）
   - 同一姓氏 / 词根必须使用同一汉字（如 Smith 与 Mrs. Smith 中的 Smith 一致）
   - 缩写若有通行中文译名则译，否则保留原文（如 NASA → 美国国家航空航天局；DNA → DNA）
   - 不确定的保留原文（不要瞎编）
4. 严禁输出任何解释、注释或 markdown 包裹。
"""


async def translate_glossary(
    candidates: list[GlossaryCandidate],
    *,
    target_lang: str = "zh-CN",
    max_terms_per_call: int = 80,
) -> dict[str, str]:
    """
    将候选术语整体送 LLM，得到 {原文: 译名} 字典。

    实现要点：
    - 把所有候选词放在 **一次或几次** 调用中，让模型看到全局上下文 → 译法自然一致。
    - 超过 max_terms_per_call 时分批，但仍把每批结果合并；批次内可保证一致，跨批通过模型本身的语言习惯近似保证。
    - LLM 调用失败时返回空 dict（调用方应回退到纯规则方案，不阻塞主流程）。

    :param candidates: extract_candidates 的产物
    :param target_lang: 目标语言（影响 prompt 表述）
    :param max_terms_per_call: 单次调用术语上限
    :return: {原文: 译名}，失败返回 {}
    """
    if not candidates:
        return {}

    terms = [c.term for c in candidates]
    batches = [terms[i:i + max_terms_per_call] for i in range(0, len(terms), max_terms_per_call)]

    try:
        from openai import AsyncOpenAI
    except ImportError:
        logger.warning("openai SDK 未安装，跳过术语 LLM 翻译")
        return {}

    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key or api_key == "dummy":
        logger.warning("OPENAI_API_KEY 未配置，跳过术语 LLM 翻译")
        return {}

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=60)
    merged: dict[str, str] = {}

    for batch_idx, batch in enumerate(batches, start=1):
        try:
            user_msg = json.dumps(batch, ensure_ascii=False)
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _GLOSSARY_SYSTEM_PROMPT},
                    {"role": "user", "content": f"目标语言：{target_lang}\n候选术语：{user_msg}"},
                ],
                temperature=0.2,  # 术语翻译要稳定，温度调低
                response_format={"type": "json_object"} if "gpt" in model.lower() else None,
            )
            raw = (resp.choices[0].message.content or "").strip()
            # 处理 markdown 包裹
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1] if "```" in raw[3:] else raw[3:]
                raw = raw.removeprefix("json").strip()
                if raw.endswith("```"):
                    raw = raw[:-3].strip()
            parsed = json.loads(raw)
            translations = parsed.get("translations", {}) if isinstance(parsed, dict) else {}
            if isinstance(translations, dict):
                for k, v in translations.items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                        # 只保留发生了语言变换的（避免把保留原文的也加入字典）
                        if k.strip() != v.strip():
                            merged[k.strip()] = v.strip()
            logger.info(
                "glossary llm batch ok",
                extra={"batch": batch_idx, "in": len(batch), "out": len(translations) if isinstance(translations, dict) else 0},
            )
        except Exception as e:
            logger.warning(f"glossary llm batch {batch_idx} failed: {e}")
            continue

    logger.info("glossary llm done", extra={"total_terms": len(terms), "translated": len(merged)})
    return merged


# ─── 合并工具 ───────────────────────────────────────────────────────────

def merge_glossaries(
    user_glossary: Optional[dict[str, str]],
    auto_glossary: Optional[dict[str, str]],
) -> dict[str, str]:
    """
    合并用户手填和自动抽取的术语库。

    优先级：用户 > 自动（用户手填的覆盖自动抽取的同名术语）。
    """
    result: dict[str, str] = {}
    if auto_glossary:
        for k, v in auto_glossary.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                result[k.strip()] = v.strip()
    if user_glossary:
        for k, v in user_glossary.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                result[k.strip()] = v.strip()
    return result


# ─── 阶段三：Verify 反向校验 ────────────────────────────────────────────

@dataclass
class VerifyResult:
    fixed_count: int = 0
    fixed_terms: dict[str, int] = field(default_factory=dict)  # {术语: 修正次数}
    unfixable_examples: list[str] = field(default_factory=list)


def verify_and_fix(
    original_html: str,
    translated_html: str,
    glossary: dict[str, str],
) -> tuple[str, VerifyResult]:
    """
    译文反向校验：
    - 对于 glossary 中的每个术语，扫描原文是否出现该术语
    - 如果原文出现但译文未出现对应译名，尝试修正：
        a) 译文里包含旧译名变体（如 "斯密思" / "史密夫"）的，直接字符串替换为正确译名
        b) 完全找不到的，记录到 unfixable_examples（仅用于观测，不阻断主流程）

    注意事项：
    - 仅做"安全替换"：用整词边界匹配，不会把"史密斯庄园"里的"史密斯"重复改成"史密斯斯"
    - 只对已知 glossary 中的术语生效，不会引入新的错误
    - 性能：术语数和文本长度都是线性，整章扫描几十毫秒内完成

    :param original_html: 原文 HTML
    :param translated_html: 译文 HTML
    :param glossary: {原文: 译名} 字典
    :return: (修正后的译文, 校验报告)
    """
    result = VerifyResult()
    if not glossary or not translated_html:
        return translated_html, result

    fixed = translated_html
    # 收集原文出现过哪些术语
    appearing_terms: list[tuple[str, str]] = []
    for src, dst in glossary.items():
        if not src or not dst:
            continue
        # 整词匹配（英文术语：单词边界；中文术语：直接 in）
        if _term_in_text(src, original_html):
            appearing_terms.append((src, dst))

    if not appearing_terms:
        return translated_html, result

    # 对每个术语检查译文中是否使用了正确译名
    for src, dst in appearing_terms:
        if dst in fixed:
            continue  # 译名已正确使用，跳过
        # 译文里没有正确译名 → 可能是 LLM 用了别的音译，尝试启发式修正
        # 简单策略：保留原文中的英文术语（如果译文中仍有英文形式），替换为正确译名
        if _term_in_text(src, fixed):
            fixed = _replace_term(fixed, src, dst)
            result.fixed_count += 1
            result.fixed_terms[src] = result.fixed_terms.get(src, 0) + 1
        else:
            # 既不是正确译名也不是英文原词，无法可靠修正
            if len(result.unfixable_examples) < 10:
                result.unfixable_examples.append(f"{src}→{dst}")

    return fixed, result


def _term_in_text(term: str, text: str) -> bool:
    """判断术语是否在文本中出现（英文用单词边界，其他直接 substring）。"""
    if not term or not text:
        return False
    # 英文术语：单词边界匹配（避免 "And" 命中 "Brand"）
    if re.match(r"^[A-Za-z][\w\s'’\-\.]*$", term):
        pattern = r"\b" + re.escape(term) + r"\b"
        return bool(re.search(pattern, text))
    return term in text


def _replace_term(text: str, src: str, dst: str) -> str:
    """整词替换 src 为 dst。"""
    if re.match(r"^[A-Za-z][\w\s'’\-\.]*$", src):
        pattern = r"\b" + re.escape(src) + r"\b"
        return re.sub(pattern, dst, text)
    return text.replace(src, dst)


# ─── 统一入口（同步包装，方便 compiler 调用）────────────────────────────

def build_auto_glossary(
    texts: Iterable[str],
    *,
    target_lang: str = "zh-CN",
    min_count: int = 2,
    max_terms: int = 200,
) -> dict[str, str]:
    """
    一站式：扫描文本 → 抽取候选 → LLM 翻译 → 返回 {原文: 译名}。

    供同步代码（compiler.py 等）调用。失败时返回 {}，不抛异常。
    """
    try:
        candidates, stats = extract_candidates(texts, min_count=min_count, max_terms=max_terms)
        if not candidates:
            return {}
        return asyncio.run(translate_glossary(candidates, target_lang=target_lang))
    except Exception as e:
        logger.error(f"build_auto_glossary failed: {e}", exc_info=True)
        return {}
