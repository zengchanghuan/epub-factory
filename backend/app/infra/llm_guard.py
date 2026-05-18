"""
LLM 模型白名单护栏 — 防止误用高价模型导致成本失控。

设计原则（呼应五大工程支柱·成本可预测）：
- Fail fast：检测到非白名单模型立即抛错，绝不静默执行
- 默认严格：白名单只包含 fixepub 实际需要的便宜模型
- 可覆盖但留痕：通过 LLM_MODEL_ALLOWLIST 环境变量可扩展，但每次变更应在 .env 留版本记录
- 黑名单兜底：即便白名单被改宽，BLOCKED_MODELS 里的高价模型永远拒绝

防御对象：
- DeepSeek：禁止 deepseek-v4-pro / deepseek-reasoner-pro / 任何带 "pro" 后缀的模型
- OpenAI：禁止 gpt-4-turbo / gpt-4 / o1-pro / claude-opus 等贵价模型
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

logger = logging.getLogger("epub_factory.llm_guard")

# ── 默认白名单（fixepub 实际使用的便宜模型）────────────────────────────────
_DEFAULT_ALLOWLIST = {
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-coder",
    "gpt-4o-mini",
}

# ── 黑名单（永远拒绝，无论白名单怎么改）────────────────────────────────────
# 触发条件：模型名包含以下任一关键词（不区分大小写）
_BLOCKED_PATTERNS = (
    "v4-pro",      # deepseek-v4-pro / deepseek-v4-pro-thinking
    "v4.5",        # 未来高级版
    "-pro",        # 兜底所有 -pro 结尾的
    "gpt-4-turbo",
    "gpt-4o-2024", # gpt-4o 完整版（贵），但允许 gpt-4o-mini
    "o1-",         # o1, o1-preview, o1-pro
    "o3-",
    "claude-opus",
    "claude-3-opus",
    "claude-sonnet",  # 明确 fixepub 不用 Claude
)


class ModelNotAllowedError(RuntimeError):
    """模型不在白名单 / 命中黑名单时抛出。"""


def _load_allowlist() -> set[str]:
    """从环境变量 LLM_MODEL_ALLOWLIST 加载白名单（逗号分隔），未配置则用默认值。"""
    raw = os.environ.get("LLM_MODEL_ALLOWLIST", "").strip()
    if not raw:
        return set(_DEFAULT_ALLOWLIST)
    parsed = {x.strip() for x in raw.split(",") if x.strip()}
    return parsed or set(_DEFAULT_ALLOWLIST)


def is_blocked(model: str) -> bool:
    """模型名是否命中黑名单关键词（最高优先级，白名单无法覆盖）。"""
    if not model:
        return False
    lower = model.lower()
    # 黑名单兜底：gpt-4o-mini 允许，gpt-4o-2024-XX 禁止
    if lower == "gpt-4o-mini":
        return False
    for kw in _BLOCKED_PATTERNS:
        if kw in lower:
            return True
    return False


def assert_model_allowed(model: str, *, context: str = "") -> None:
    """
    校验单个模型名是否允许调用。

    Args:
        model: 模型名，如 "deepseek-chat"
        context: 调用方标识，仅用于日志，如 "translator" / "polish"

    Raises:
        ModelNotAllowedError: 模型被黑名单拦截或不在白名单
    """
    if not model:
        raise ModelNotAllowedError(
            f"[llm_guard] empty model name (context={context!r})"
        )

    # 1) 黑名单优先：永远拒绝（即便手工加进白名单也无效）
    if is_blocked(model):
        msg = (
            f"[llm_guard] BLOCKED model '{model}' (context={context!r}); "
            f"this model is too expensive for fixepub and must not be used. "
            f"Allowed: deepseek-chat / gpt-4o-mini."
        )
        logger.error(msg)
        raise ModelNotAllowedError(msg)

    # 2) 白名单校验
    allowlist = _load_allowlist()
    if model not in allowlist:
        msg = (
            f"[llm_guard] model '{model}' not in allowlist {sorted(allowlist)} "
            f"(context={context!r}). Set LLM_MODEL_ALLOWLIST env to extend if intended."
        )
        logger.error(msg)
        raise ModelNotAllowedError(msg)


def assert_models_allowed(models: Iterable[str], *, context: str = "") -> None:
    """批量校验：主模型 + fallback 列表。"""
    for m in models:
        if m:
            assert_model_allowed(m, context=context)


def safe_model_or_default(model: str, default: str = "deepseek-chat") -> str:
    """
    柔性兜底：检测到不允许的模型，返回 default 而非抛错。
    适用于「检测+自动降级」场景，但默认仍推荐 fail fast。
    """
    try:
        assert_model_allowed(model)
        return model
    except ModelNotAllowedError:
        logger.warning("[llm_guard] downgrade '%s' -> '%s'", model, default)
        return default
