"""
L4 LLM 精校 — DeepSeek 精校两岸歧义词

架构：
- 仅对命中 risky.yaml 风险词的段落调用 DeepSeek
- 以「段落」为单位送入（保留上下文），而非全文跑 LLM
- 6 道护栏防止成本失控（见注释）
- 任意失败自动降级到 L3 输出，并设置 refund_required 标志
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .lexicon_matcher import LexiconMatcher

logger = logging.getLogger("epub_factory.l4")

# DeepSeek API 基础配置（通过环境变量注入）
_DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 护栏：单本最大 token 上限（token 超限后静默停止 L4 + 标记退款）
# 设置为 200 万 token（内部硬限，用户界面不暴露此限制）
_MAX_TOKENS_PER_BOOK = int(os.environ.get("L4_MAX_TOKENS_PER_BOOK", "2000000"))

# 护栏：单段最大 token 数（超过则跳过该段）
_MAX_TOKENS_PER_PARAGRAPH = int(os.environ.get("L4_MAX_TOKENS_PER_PARA", "4096"))

# 护栏：批量请求最大段落数
_MAX_PARAGRAPHS_PER_BATCH = int(os.environ.get("L4_MAX_PARAS_PER_BATCH", "20"))

# 护栏：单次 API 调用超时（秒）
_API_TIMEOUT = float(os.environ.get("L4_API_TIMEOUT_SEC", "30"))

_PROMPT_SYSTEM = """你是繁简转换的「中国大陆习惯用语」精校器。
任务：将下面的繁体/台湾中文段落转换为符合大陆习惯的简体中文，仅修改用词，不改动语义和文体。
约束：
1. 保留原段落的 HTML 标签结构（如 <p>、<em>、<strong>）
2. 仅替换两岸用词差异（如「窩心→暖心」、「機車→摩托车」），不做翻译或改写
3. 返回纯文本，与输入等长，不要添加解释或额外内容
4. 保持原始换行和空格"""

_PROMPT_USER_TEMPLATE = "请精校以下段落：\n\n{paragraph}"

# HTML 标签分割正则，用于提取段落
_PARA_RE = re.compile(r"<p[^>]*>.*?</p>", re.DOTALL | re.IGNORECASE)


@dataclass
class L4Stats:
    enabled: bool = False
    model: str = ""
    paragraphs_sent: int = 0
    paragraphs_polished: int = 0
    paragraphs_fallback: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cny: float = 0.0
    fallback: bool = False       # 是否发生过降级
    refund_required: bool = False  # 是否需要退款

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "paragraphs_sent": self.paragraphs_sent,
            "paragraphs_polished": self.paragraphs_polished,
            "paragraphs_fallback": self.paragraphs_fallback,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_cny": round(self.cost_cny, 4),
            "fallback": self.fallback,
            "refund_required": self.refund_required,
        }


def _estimate_tokens(text: str) -> int:
    """粗估 token 数（中文约 1.5 字/token，英文约 4 字/token）。"""
    return max(1, len(text) // 2)


def _calc_cost(tokens_in: int, tokens_out: int) -> float:
    """
    DeepSeek-chat 价目（2026 年）：
    输入 ¥1/M token，输出 ¥4/M token
    """
    return tokens_in / 1_000_000 * 1.0 + tokens_out / 1_000_000 * 4.0


class LLMPolisher:
    """
    L4 DeepSeek 精校器。

    用法：
        polisher = LLMPolisher(api_key="sk-xxx")
        polished_html, stats = polisher.polish_html(html_text)
    """

    def __init__(self, api_key: Optional[str] = None):
        # 与其他 LLM 模块保持一致：优先 DEEPSEEK_API_KEY，兜底 OPENAI_API_KEY
        # （DeepSeek 走 OpenAI 兼容协议，整个项目复用同一把 key）
        self._api_key = (
            api_key
            or os.environ.get("DEEPSEEK_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", "")
        )
        self._risky_words: List[str] = LexiconMatcher.get_risky_words()
        self._risky_pattern: Optional[re.Pattern] = None
        if self._risky_words:
            # 按长度降序，优先匹配长词
            sorted_words = sorted(self._risky_words, key=len, reverse=True)
            escaped = [re.escape(w) for w in sorted_words if w]
            self._risky_pattern = re.compile("|".join(escaped))

    def _has_risky_word(self, text: str) -> bool:
        if not self._risky_pattern:
            return False
        return bool(self._risky_pattern.search(text))

    def _call_deepseek(self, paragraph: str) -> Optional[str]:
        """调用 DeepSeek API 精校单段，失败返回 None。"""
        if not self._api_key:
            logger.warning("L4: DEEPSEEK_API_KEY / OPENAI_API_KEY not set, skipping")
            return None

        # 模型白名单护栏：禁止 deepseek-v4-pro 等高价模型
        from app.infra.llm_guard import assert_model_allowed, ModelNotAllowedError
        try:
            assert_model_allowed(_DEEPSEEK_MODEL, context="polish")
        except ModelNotAllowedError as exc:
            logger.error("L4 skipped: %s", exc)
            return None

        try:
            import httpx
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": _DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": _PROMPT_SYSTEM},
                    {"role": "user", "content": _PROMPT_USER_TEMPLATE.format(paragraph=paragraph)},
                ],
                "temperature": 0.1,
                "max_tokens": min(_MAX_TOKENS_PER_PARAGRAPH, 4096),
            }
            with httpx.Client(timeout=_API_TIMEOUT) as client:
                resp = client.post(
                    f"{_DEEPSEEK_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            if resp.status_code != 200:
                logger.warning("L4: DeepSeek API error %s: %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning("L4: DeepSeek call failed: %s", exc)
            return None

    def _call_deepseek_with_retry(self, paragraph: str) -> Optional[str]:
        """带一次重试的 DeepSeek 调用。"""
        result = self._call_deepseek(paragraph)
        if result is None:
            time.sleep(1.0)
            result = self._call_deepseek(paragraph)
        return result

    def polish_html(self, html_text: str) -> tuple[str, L4Stats]:
        """
        对整段 HTML 进行 L4 精校。
        - 仅对含风险词的 <p> 段落调用 LLM
        - 超 token 护栏时全体段落降级，标记 refund_required

        返回: (polished_html, stats)
        """
        stats = L4Stats(enabled=True, model=_DEEPSEEK_MODEL)
        total_tokens_used = 0

        if not self._risky_pattern:
            logger.info("L4: no risky words loaded, skipping")
            return html_text, stats

        # 提取所有 <p> 段落及其位置
        paragraphs: list[tuple[int, int, str]] = []  # (start, end, text)
        for m in _PARA_RE.finditer(html_text):
            para_text = m.group(0)
            if self._has_risky_word(para_text):
                paragraphs.append((m.start(), m.end(), para_text))

        if not paragraphs:
            return html_text, stats

        stats.paragraphs_sent = len(paragraphs)
        logger.info("L4: found %d risky paragraphs", len(paragraphs))

        # 护栏：单本超 token 限额
        for start, end, para_text in paragraphs:
            est_tokens = _estimate_tokens(para_text)
            if total_tokens_used + est_tokens > _MAX_TOKENS_PER_BOOK:
                logger.warning("L4: token limit reached, stopping polish + marking refund")
                stats.fallback = True
                stats.refund_required = True
                break

            polished = self._call_deepseek_with_retry(para_text)
            if polished is None:
                stats.paragraphs_fallback += 1
                stats.fallback = True
                logger.info("L4: paragraph fallback at pos %d", start)
                continue

            tokens_in = _estimate_tokens(para_text)
            tokens_out = _estimate_tokens(polished)
            total_tokens_used += tokens_in + tokens_out
            stats.tokens_in += tokens_in
            stats.tokens_out += tokens_out
            stats.cost_cny += _calc_cost(tokens_in, tokens_out)
            stats.paragraphs_polished += 1

            # 替换原段落
            html_text = html_text[:start] + polished + html_text[end:]

        stats.cost_cny = round(stats.cost_cny, 4)
        logger.info(
            "L4: done polished=%d fallback=%d cost=¥%.4f",
            stats.paragraphs_polished, stats.paragraphs_fallback, stats.cost_cny,
        )
        return html_text, stats


def count_effective_chars(epub_path: str) -> int:
    """
    统计 EPUB 正文有效字数（去除目录、nav、样式、空白与重复元数据）。
    用于前端报价前预估 AI 精校费用。
    """
    import zipfile
    from html.parser import HTMLParser

    class _Strip(HTMLParser):
        def __init__(self):
            super().__init__()
            self._parts: list[str] = []
            self._in_skip = 0

        def handle_starttag(self, tag, attrs):
            if tag.lower() in ("script", "style", "nav"):
                self._in_skip += 1

        def handle_endtag(self, tag):
            if tag.lower() in ("script", "style", "nav"):
                self._in_skip = max(0, self._in_skip - 1)

        def handle_data(self, data):
            if self._in_skip == 0:
                self._parts.append(data)

        def text(self) -> str:
            return "".join(self._parts)

    total = 0
    seen_texts: set[int] = set()
    try:
        with zipfile.ZipFile(epub_path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
            # 跳过明显是 nav/toc 文件（按文件名）
            names = [n for n in names if not any(
                kw in n.lower() for kw in ("nav", "toc", "contents", "index")
            )]
            for name in names:
                try:
                    raw = zf.read(name).decode("utf-8", errors="ignore")
                    parser = _Strip()
                    parser.feed(raw)
                    text = parser.text().strip()
                    text_hash = hash(text[:500])  # 用前 500 字做去重
                    if text_hash in seen_texts:
                        continue
                    seen_texts.add(text_hash)
                    # 统计中文字符数（更贴近「字数」概念）
                    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
                    total += cjk_count or len(text.replace(" ", "").replace("\n", ""))
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("count_effective_chars failed: %s", exc)
    return total


def calculate_polish_price(char_count: int) -> float:
    """
    根据正文有效字数（中文字符数）计算 AI 精校价格（元）。
    与 docs/PRODUCT-STRATEGY.md 中阶梯计价保持一致：
      ≤ 15 万字 → ¥3.99
      15-30 万字 → ¥5.99
      30-60 万字 → ¥8.99
      60-100 万字 → ¥12.99
      > 100 万字 → ¥12.99 + 每增 50 万字 +¥4
    """
    if char_count <= 150_000:
        return 3.99
    if char_count <= 300_000:
        return 5.99
    if char_count <= 600_000:
        return 8.99
    if char_count <= 1_000_000:
        return 12.99
    extra_50w = (char_count - 1_000_000 + 499_999) // 500_000
    return round(12.99 + extra_50w * 4.0, 2)
