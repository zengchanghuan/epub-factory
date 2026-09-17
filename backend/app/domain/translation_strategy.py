"""Deterministic translation strategy assets and routing helpers.

The profiler may recommend one of these strategies, but it cannot invent
arbitrary instructions.  Keeping the prompt fragments here makes routing
reviewable, versioned, and independently testable.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

TRANSLATION_STRATEGY_VERSION = "strategy-v1"

TRANSLATION_STRATEGIES = (
    "neutral_faithful",
    "literary_narrative",
    "academic_rigorous",
    "mirror_fidelity",
    "practical_technical",
)
TRANSLATION_STRATEGY_CHOICES = {"auto", *TRANSLATION_STRATEGIES}

TRANSLATION_STRATEGY_LABELS = {
    "auto": "自动判断",
    "neutral_faithful": "中性忠实",
    "literary_narrative": "文学叙事",
    "academic_rigorous": "学术严谨",
    "mirror_fidelity": "镜像级忠实",
    "practical_technical": "实用技术",
}

_STRATEGY_PROMPTS = {
    "neutral_faithful": """
本书采用“中性忠实”策略：
- 以准确、清晰和稳定为先，只做符合目标语言基本语法的必要调整。
- 保留原文事实、论证顺序、语气强度和表达边界，不增写解释或总结。
- 类型或语境不明确时保持克制，不凭常识补全作者没有说出的内容。
""",
    "literary_narrative": """
本书采用“文学叙事”策略：
- 在不改变事实、人物关系、叙事视角和语气的前提下，使对话自然、人物声音连贯。
- 保留场景氛围、节奏、意象、讽刺和情绪强度；可按中文习惯调整语序。
- 只有在长句直接妨碍中文阅读时才可谨慎拆句，禁止增写情节、心理或解释。
""",
    "academic_rigorous": """
本书采用“学术严谨”策略：
- 术语、定义和命题必须前后一致，完整保留限定条件、否定、因果和论证层级。
- 不得为了通俗而简化概念、弱化争议或跳过看似重复的推导。
- 引文、文献称谓、公式、变量、编号及跨段指代必须精确。
""",
    "mirror_fidelity": """
本书采用“镜像级忠实”策略，绝对忠实优先于流畅和通俗：
- 保留比喻、排比、外交辞令、讽刺、情绪强度、刻意含糊和时代语感。
- 在符合目标语言基本语法的底线之上，尽量保持逻辑顺序、从句嵌套和结构张力。
- 零增删：禁止添加解释、背景、总结，禁止省略过渡词或替作者修正观点。
- 禁止用现代网络文学、营销文案或轻快口吻改写历史、政治、哲学或法律文本。
""",
    "practical_technical": """
本书采用“实用技术”策略：
- 操作步骤、警告、前置条件、单位、参数、变量和名词必须准确且前后一致。
- 优先使用清晰、可执行的目标语言表达，但不得省略限制条件或安全提示。
- 代码、命令、接口名、路径、占位符和产品专名保持原样，除非术语表明确指定译法。
""",
}


def normalize_translation_strategy(
    value: str | None,
    *,
    allow_auto: bool = True,
    default: str = "auto",
) -> str:
    normalized = str(value or default).strip().lower()
    allowed = TRANSLATION_STRATEGY_CHOICES if allow_auto else set(TRANSLATION_STRATEGIES)
    if normalized not in allowed:
        return default if default in allowed else "neutral_faithful"
    return normalized


def resolve_translation_strategy(
    requested: str | None,
    profile: dict[str, Any] | None,
) -> tuple[str, str]:
    """Return ``(resolved, source)`` with explicit user choice taking priority."""
    normalized = normalize_translation_strategy(requested)
    if normalized != "auto":
        return normalized, "user"

    recommended = normalize_translation_strategy(
        (profile or {}).get("recommended_strategy"),
        allow_auto=False,
        default="neutral_faithful",
    )
    if (profile or {}).get("status") == "ok":
        return recommended, "profiler"
    return recommended, "fallback"


def strategy_prompt(strategy: str | None) -> str:
    normalized = normalize_translation_strategy(
        strategy,
        allow_auto=False,
        default="neutral_faithful",
    )
    return _STRATEGY_PROMPTS[normalized].strip()


def strategy_allows_literary_polish(strategy: str | None) -> bool:
    """Only strategies with non-minimal editorial permission may rewrite prose."""
    normalized = normalize_translation_strategy(
        strategy,
        allow_auto=False,
        default="neutral_faithful",
    )
    return normalized in {"neutral_faithful", "literary_narrative"}


def strategy_recommends_bilingual(strategy: str | None) -> bool:
    return normalize_translation_strategy(
        strategy,
        allow_auto=False,
        default="neutral_faithful",
    ) == "mirror_fidelity"


def compact_profile_context(profile: dict[str, Any] | None, *, max_chars: int = 2600) -> str:
    """Build a compact, evidence-bounded context for the System Prompt."""
    if not profile:
        return ""
    complexity = profile.get("complexity") if isinstance(profile.get("complexity"), dict) else {}
    narrative = profile.get("narrative") if isinstance(profile.get("narrative"), dict) else {}
    context = {
        "genre": profile.get("genre") or "unknown",
        "subgenres": list(profile.get("subgenres") or [])[:6],
        "tone": list(profile.get("tone") or [])[:6],
        "complexity": {
            "source_level": complexity.get("source_level") or "unknown",
            "syntax": complexity.get("syntax") or "unknown",
            "terminology_density": complexity.get("terminology_density") or "unknown",
        },
        "narrative": {
            "person": narrative.get("person") or "unknown",
            "dialogue_ratio": narrative.get("dialogue_ratio") or "unknown",
            "rhetorical_features": list(narrative.get("rhetorical_features") or [])[:6],
        },
        "fidelity_risk": profile.get("fidelity_risk") or "unknown",
        "book_summary": str(profile.get("book_summary") or "")[:800],
    }
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))[:max_chars]


def profile_cache_hash(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "none"
    stable = {
        "genre": profile.get("genre"),
        "subgenres": profile.get("subgenres"),
        "tone": profile.get("tone"),
        "complexity": profile.get("complexity"),
        "narrative": profile.get("narrative"),
        "fidelity_risk": profile.get("fidelity_risk"),
        "recommended_strategy": profile.get("recommended_strategy"),
        "book_summary": profile.get("book_summary"),
        "characters": profile.get("characters"),
        "profiler_version": profile.get("profiler_version"),
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def relevant_character_context(
    profile: dict[str, Any] | None,
    source_text: str,
    *,
    max_characters: int = 8,
    max_chars: int = 1600,
) -> str:
    """Select only character records whose names or aliases occur in this chunk."""
    characters = (profile or {}).get("characters")
    if not isinstance(characters, list) or not source_text:
        return ""
    folded = source_text.casefold()
    selected: list[dict[str, Any]] = []
    for raw in characters:
        if not isinstance(raw, dict):
            continue
        names = [
            str(raw.get("source_name") or "").strip(),
            *[str(alias).strip() for alias in (raw.get("aliases") or [])],
        ]
        names = [name for name in names if name]
        if not names or not any(name.casefold() in folded for name in names):
            continue
        selected.append({
            "source_name": names[0],
            "translated_name": str(raw.get("translated_name") or "")[:100],
            "aliases": names[1:6],
            "role": str(raw.get("role") or "unknown")[:180],
            "pronouns": str(raw.get("pronouns") or "unknown")[:60],
            "relationships": [
                str(value)[:160]
                for value in (raw.get("relationships") or [])[:6]
            ],
            "confidence": raw.get("confidence"),
        })
        if len(selected) >= max_characters:
            break
    if not selected:
        return ""
    return json.dumps(selected, ensure_ascii=False, separators=(",", ":"))[:max_chars]


def build_readonly_chapter_summary(
    file_path: str,
    html_chunks: list[str],
    *,
    max_chars: int = 900,
) -> str:
    """Create a deterministic extractive synopsis before any translations run."""
    visible: list[str] = []
    for chunk in html_chunks:
        text = BeautifulSoup(str(chunk or ""), "html.parser").get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            visible.append(text)
    if not visible:
        return f"章节文件：{file_path}"
    indexes = sorted({0, len(visible) // 2, len(visible) - 1})
    samples = [visible[index][:260] for index in indexes]
    joined = " / ".join(samples)
    return f"章节文件：{file_path}\n章节原文抽样提要（只读，不是待翻译正文）：{joined}"[:max_chars]


# Local import kept at the end so the strategy constants remain lightweight.
from bs4 import BeautifulSoup  # noqa: E402
