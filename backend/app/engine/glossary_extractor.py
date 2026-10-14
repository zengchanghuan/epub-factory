"""
全书术语库抽取引擎。

解决 MapReduce 翻译范式下，跨 chunk/章节的人名、地名、专有名词译法不一致问题。

两阶段流程：
    1) extract_candidates(book)     纯规则层：正则扫描原文，统计候选名词频次
    2) translate_glossary(candidates, llm) LLM 层：把候选词统一翻译成目标语言

调用者负责把产出的 {src: dst} 字典注入 SemanticsTranslator 的 glossary 字段。
"""
from __future__ import annotations
from app.infra.llm_gateway import governed_request, preflight_output_limit

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlsplit
from app.infra.llm_errors import ProviderAccountUnavailable, is_balance_error
from app.cancellation import JobCancelled, raise_if_cancelled
from app.domain.translation_terms import relevant_terms, term_spans, visible_term_text, replace_term_text, parenthetical_spans
from billiard.exceptions import SoftTimeLimitExceeded

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

# Preserve lowercase name particles inside a capitalized phrase. Do not join
# arbitrary lowercase prose ("Alice met Bob") or cross sentence punctuation.
_NAME_WORD = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’\-]{1,}"
_NAME_PARTICLE = r"(?:van|von|de|del|della|di|da|dos|du|la|le|den|der|ten|ter)"
_NAME_PHRASE = _NAME_WORD + r"(?:\s+(?:" + _NAME_PARTICLE + r"\s+){0,3}" + _NAME_WORD + r"){0,5}"
_CAPITALIZED_RUN_RE = re.compile(r"\b(" + _NAME_PHRASE + r")\b")
_HONORIFIC_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in _HONORIFICS) + r")\s+(" + _NAME_PHRASE + r")\b"
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
    contexts: list[str] = field(default_factory=list)


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
    contexts_by_term: dict[str, list[str]] = {}
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
                _append_context(contexts_by_term, term, text, *m.span(1))

        # 连续大写词
        for m in _CAPITALIZED_RUN_RE.finditer(text):
            term = _normalize(m.group(1))
            if _is_valid_term(term):
                counter[term] += 1
                kinds_by_term.setdefault(term, set()).add("capitalized")
                _append_context(contexts_by_term, term, text, *m.span(1))

        # 缩写
        for m in _ACRONYM_RE.finditer(text):
            term = m.group(1)
            if len(term) >= 2 and term not in _SENTENCE_INITIAL_BLACKLIST:
                counter[term] += 1
                kinds_by_term.setdefault(term, set()).add("acronym")
                _append_context(contexts_by_term, term, text, *m.span(1))

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
            contexts=list(contexts_by_term.get(term, [])),
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


def _append_context(
    contexts_by_term: dict[str, list[str]],
    term: str,
    text: str,
    start: int,
    end: int,
    *,
    limit: int = 2,
) -> None:
    samples = contexts_by_term.setdefault(term, [])
    if len(samples) >= limit:
        return
    left = max(0, start - 80)
    right = min(len(text), end + 80)
    sample = re.sub(r"\s+", " ", text[left:right]).strip()
    if sample and sample not in samples:
        samples.append(sample)


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
1. 输入是候选术语对象数组，每项包含 term、kind 和原书 context。必须结合上下文判断它是否真是专名。
2. 输出 JSON 对象，格式必须为：{"translations": {"原文": "译名", ...}}
3. 翻译原则：
   - 人名采用通行的音译（如 Harry → 哈利、Smith → 史密斯）
   - 地名采用通行译法（如 London → 伦敦、Hogwarts → 霍格沃茨）
   - 同一姓氏 / 词根必须使用同一汉字（如 Smith 与 Mrs. Smith 中的 Smith 一致）
   - 缩写若有通行中文译名则译，否则保留原文（如 NASA → 美国国家航空航天局；DNA → DNA）
   - 普通名词、形容词、国籍形容词和依语境变化的词不要收入术语表，保持原文作为值
   - 优先保留完整短语，不要用短词覆盖更具体的长词组
   - 不确定的保留原文（不要瞎编）
4. 严禁输出任何解释、注释或 markdown 包裹。
"""


async def translate_glossary(
    candidates: list[GlossaryCandidate],
    *,
    target_lang: str = "zh-CN",
    max_terms_per_call: int = 80,
    metrics: dict | None = None,
    cancel_check=None,
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

    entries = [
        {
            "term": c.term,
            "kind": sorted(c.kinds),
            "contexts": list(c.contexts[:2]),
        }
        for c in candidates
    ]
    max_terms_per_call = max(1, max_terms_per_call)
    batches = [entries[i:i + max_terms_per_call] for i in range(0, len(entries), max_terms_per_call)]

    try:
        from openai import AsyncOpenAI
    except ImportError:
        if metrics is not None:
            metrics["llm_batches_failed"] = len(batches)
        logger.warning("openai SDK 未安装，跳过术语 LLM 翻译")
        return {}

    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = (os.environ.get("EPUB_DEFAULT_TRANSLATION_MODEL")
             or os.environ.get("OPENAI_MODEL", "deepseek-flash"))
    if not api_key or api_key == "dummy":
        logger.warning("OPENAI_API_KEY 未配置，跳过术语 LLM 翻译")
        return {}
    # 模型白名单护栏：拒绝高价模型，命中则跳过术语抽取而非阻塞主流程
    from app.infra.llm_guard import assert_model_allowed, ModelNotAllowedError
    try:
        assert_model_allowed(model, context="glossary")
    except ModelNotAllowedError as exc:
        if metrics is not None:
            metrics["llm_batches_failed"] = len(batches)
        logger.warning("glossary 跳过：%s", exc)
        return {}

    # 显式传入自建 http_client：避免 openai SDK 内部向新版 httpx(>=0.28) 传递已被
    # 移除的 proxies 参数而抛 TypeError（与 SemanticsTranslator 的构造方式保持一致）。
    import httpx
    output_limit = preflight_output_limit()
    request_timeout = max(1.0, float(os.environ.get("EPUB_GLOSSARY_REQUEST_TIMEOUT", "300")))
    http_client = httpx.AsyncClient(timeout=min(60, request_timeout))
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0, http_client=http_client)
    merged: dict[str, str] = {}
    concurrency = max(1, min(4, int(os.environ.get("EPUB_GLOSSARY_CONCURRENCY", "2"))))
    stats = metrics if metrics is not None else {}
    stats.update(llm_batches_total=len(batches), llm_batches_failed=0,
                 llm_batches_successful=0, llm_concurrency=concurrency, llm_api_calls=0)
    started = time.monotonic()
    blocked = None
    queue = asyncio.Queue()
    for item in enumerate(batches, start=1):
        queue.put_nowait(item)

    async def worker():
        nonlocal blocked
        while not queue.empty():
            raise_if_cancelled(cancel_check)
            if blocked:
                raise blocked
            batch_idx, batch = queue.get_nowait()
            try:
                user_msg = json.dumps(batch, ensure_ascii=False)
                batch_terms = {item["term"] for item in batch}
                messages = [
                    {"role": "system", "content": _GLOSSARY_SYSTEM_PROMPT},
                    {"role": "user", "content": f"目标语言：{target_lang}\n候选术语：{user_msg}"},
                ]
                def before_dispatch():
                    stats["llm_api_calls"] += 1
                resp = await governed_request(lambda: client.chat.completions.create(
                    model=model, messages=messages, max_tokens=output_limit,
                    temperature=0.2,  # 术语翻译要稳定，温度调低
                    response_format={"type": "json_object"} if "gpt" in model.lower() else None,
                ), model=model, base_url=base_url, messages=messages, stage="glossary",
                    max_output_tokens=output_limit, timeout=request_timeout,
                    cancel_check=cancel_check, before_dispatch=before_dispatch)
                raw = (resp.choices[0].message.content or "").strip()
                # 处理 markdown 包裹
                if raw.startswith("```"):
                    raw = raw.split("```", 2)[1] if "```" in raw[3:] else raw[3:]
                    raw = raw.removeprefix("json").strip()
                    if raw.endswith("```"):
                        raw = raw[:-3].strip()
                parsed = json.loads(raw)
                if not isinstance(parsed, dict) or "translations" not in parsed:
                    raise ValueError("glossary response requires a translations object")
                translations = parsed["translations"]
                if isinstance(translations, dict):
                    for k, v in translations.items():
                        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                            # 只保留发生了语言变换的（避免把保留原文的也加入字典）
                            if k.strip() in batch_terms and k.strip() != v.strip():
                                merged[k.strip()] = v.strip()
                else:
                    raise ValueError("glossary translations must be a JSON object")
                stats["llm_batches_successful"] += 1
                logger.info(
                    "glossary llm batch ok",
                    extra={"batch": batch_idx, "in": len(batch), "out": len(translations) if isinstance(translations, dict) else 0},
                )
            except (JobCancelled, SoftTimeLimitExceeded, ProviderAccountUnavailable):
                raise
            except Exception as e:
                if is_balance_error(e):
                    blocked = ProviderAccountUnavailable(urlsplit(base_url).hostname or "primary")
                    raise blocked from e
                stats["llm_batches_failed"] += 1
                # Do not include model responses or candidate book text in logs.
                logger.warning("glossary llm batch %s failed (%s)", batch_idx, type(e).__name__)
                continue
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(batches)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stats["llm_elapsed_ms"] = round((time.monotonic() - started) * 1000)
        try:
            await http_client.aclose()
        except Exception:
            pass

    logger.info("glossary llm done", extra={"total_terms": len(entries), "translated": len(merged)})
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
    *,
    glossary_catalog: list[dict] | None = None,
) -> tuple[str, VerifyResult]:
    """Replace only residual source terms in editable text, never infer aliases.

    Unknown Chinese variants remain review signals. Attributes, code, media,
    comments, URLs/DOIs and preserved bilingual originals are never rewritten.
    """
    result = VerifyResult()
    relevant = select_relevant_glossary(glossary, visible_term_text(original_html))
    if not relevant or not translated_html:
        return translated_html, result
    target_text = visible_term_text(translated_html)
    from app.domain.translation_terminology_policy import terminology_evidence
    evidence = terminology_evidence(visible_term_text(original_html), target_text, relevant, glossary_catalog)
    pending = {row["source_term"]: row["expected_translation"] for row in evidence if row["category"] != "matched"}
    ambiguous = {row["source_term"] for row in evidence if row["category"] in {
        "glossary_mapping_ambiguous", "glossary_occurrence_unresolved"}}
    # A target that is another source term is ambiguous in an unaligned
    # paragraph. Do not guess which occurrence already represents which name.
    collisions = {term for src, dst in relevant.items() for other in relevant
                  if src != other and term_spans(dst, other) == [(0, len(other))]
                  for term in (src, other)}
    protected = re.compile(r"(?:https?://|www\.)[^\s<>]+|(?:doi:\s*)?10\.\d{4,9}/[^\s<>]+", re.I)
    # Decide replacements on original text once; never reserialize HTML.
    def edits_for_text(text, is_editable):
        excluded = [m.span() for m in protected.finditer(text)]
        parentheses = parenthetical_spans(text)
        edits = []
        for src, dst in relevant_terms(relevant, text).items():
            if src not in pending or src in collisions or src in ambiguous:
                continue
            for start, end in term_spans(src, text):
                if not is_editable(start, end):
                    continue
                if any(start < b and end > a for a, b in excluded):
                    continue
                # Retained original names in annotations are not untranslated
                # prose. Never replace them with a second Chinese spelling.
                if any("LATIN" in unicodedata.name(c, "") for c in src) and any(
                        start >= a and end <= b for a, b in parentheses):
                    continue
                # Recheck overlap per occurrence, including a longer phrase
                # whose canonical translation is already present elsewhere.
                longer = [span for term in relevant if len(term) > len(src)
                          for span in term_spans(term, text)]
                if any(start < b and end > a for a, b in longer):
                    continue
                edits.append((start, end, dst, src))
        for _, _, _, src in edits:
            result.fixed_count += 1
            result.fixed_terms[src] = result.fixed_terms.get(src, 0) + 1
        return [(start, end, dst) for start, end, dst, _ in edits]

    fixed = replace_term_text(translated_html, edits_for_text)
    final_text = visible_term_text(fixed)
    for finding in terminology_evidence(visible_term_text(original_html), final_text, pending, glossary_catalog):
        if finding["category"] != "matched" and len(result.unfixable_examples) < 10:
            result.unfixable_examples.append(f"{finding['source_term']}→{finding['expected_translation']}")
    return fixed, result


def _term_in_text(term: str, text: str) -> bool:
    return bool(term_spans(term, text))


def select_relevant_glossary(
    glossary: dict[str, str], text: str, *, max_terms: int = 32,
) -> dict[str, str]:
    return relevant_terms(glossary, text, limit=max_terms)


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
