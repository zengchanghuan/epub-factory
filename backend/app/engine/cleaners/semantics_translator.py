import asyncio
import html as html_lib
import json
import random
import re
import os
import time
from dataclasses import dataclass, field
from bs4 import BeautifulSoup, Tag, NavigableString
from openai import AsyncOpenAI
import httpx
from app.cancellation import raise_if_cancelled
from ..translation_cache import TranslationCache


# 定价：每百万 token 美元（来源：DeepSeek / OpenAI 公开价格，可随官网更新）
PRICING = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-v3": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}

BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


@dataclass
class TranslationStats:
    total_chunks: int = 0
    cached_chunks: int = 0
    api_calls: int = 0
    translated_chunks: int = 0
    failed_chunks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    start_time: float = field(default_factory=time.time)
    errors: int = 0
    connection_errors: int = 0
    timeout_errors: int = 0
    retry_attempts: int = 0
    last_error: str = ""
    api_latency_ms_total: int = 0
    api_latency_ms_max: int = 0
    api_latency_samples: int = 0
    quality_fallback_attempts: int = 0
    quality_fallback_model: str = ""
    complex_chunks: int = 0
    complex_singleton_batches: int = 0
    inline_tag_repairs: int = 0

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    def estimate_cost(self, model: str) -> float:
        prices = PRICING.get(model, {"input": 0.0, "output": 0.0})
        input_cost = (self.prompt_tokens / 1_000_000) * prices["input"]
        output_cost = (self.completion_tokens / 1_000_000) * prices["output"]
        return input_cost + output_cost

    def summary(self, model: str) -> str:
        cost = self.estimate_cost(model)
        return (
            f"\n{'=' * 50}\n"
            f"📊 翻译统计报告\n"
            f"{'=' * 50}\n"
            f"  模型: {model}\n"
            f"  总段落数: {self.total_chunks}\n"
            f"  命中缓存: {self.cached_chunks}\n"
            f"  API 调用: {self.api_calls}\n"
            f"  成功写入: {self.translated_chunks}\n"
            f"  失败次数: {self.errors}\n"
            f"  ─────────────────────\n"
            f"  Prompt Tokens:     {self.prompt_tokens:,}\n"
            f"  Completion Tokens: {self.completion_tokens:,}\n"
            f"  Total Tokens:      {self.total_tokens:,}\n"
            f"  ─────────────────────\n"
            f"  预估费用: ${cost:.4f} USD\n"
            f"  耗时: {self.elapsed_seconds:.1f}s\n"
            f"{'=' * 50}"
        )

    def to_dict(self, model: str) -> dict:
        return {
            "model": model,
            "total_chunks": self.total_chunks,
            "cached_chunks": self.cached_chunks,
            "api_calls": self.api_calls,
            "translated_chunks": self.translated_chunks,
            "failed_chunks": self.failed_chunks,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "errors": self.errors,
            "connection_errors": self.connection_errors,
            "timeout_errors": self.timeout_errors,
            "retry_attempts": self.retry_attempts,
            "api_latency_ms_total": self.api_latency_ms_total,
            "api_latency_ms_max": self.api_latency_ms_max,
            "api_latency_samples": self.api_latency_samples,
            "quality_fallback_attempts": self.quality_fallback_attempts,
            "quality_fallback_model": self.quality_fallback_model,
            "complex_chunks": self.complex_chunks,
            "complex_singleton_batches": self.complex_singleton_batches,
            "inline_tag_repairs": self.inline_tag_repairs,
            "cost_usd": self.estimate_cost(model),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "last_error": self.last_error,
            "all_failed": (self.translated_chunks + self.cached_chunks) == 0 and self.failed_chunks > 0,
        }


@dataclass
class SingleChunkResult:
    """单 chunk 翻译结果，供持久化与统计。"""
    translated_html: str
    cached: bool
    model: str | None
    base_url: str | None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    error: str | None
    retry_count: int = 0
    error_type: str | None = None


class SemanticsTranslator:
    def __init__(self, target_lang="zh-CN", concurrency=5, bilingual=False,
                 glossary: dict | None = None, temperature: float | None = None,
                 model: str | None = None, quality_fallback_model: str | None = None):
        self.target_lang = target_lang
        self.bilingual = bilingual
        self.glossary: dict[str, str] = glossary or {}
        self.cache = TranslationCache()
        self.progress_callback = None
        self.cancel_check = None
        configured_batch_max = int(os.environ.get("EPUB_TRANSLATION_BATCH_MAX_CHARS", self._BATCH_MAX_CHARS))
        batch_cap = int(os.environ.get("EPUB_TRANSLATION_BATCH_MAX_CHARS_CAP", "6000"))
        self.batch_max_chars = max(1000, min(configured_batch_max, batch_cap))

        self.api_key = os.environ.get("OPENAI_API_KEY", "dummy")
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = (model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")).strip()
        self.model_fallbacks = self._parse_csv_env("OPENAI_MODEL_FALLBACKS")
        if quality_fallback_model is None:
            quality_fallback_model = os.environ.get("EPUB_TRANSLATION_QUALITY_FALLBACK_MODEL", "deepseek-v4-pro")
        self.quality_fallback_model = (quality_fallback_model or "").strip()
        self.pro_fallback_after_retries = max(0, int(os.environ.get("EPUB_TRANSLATION_PRO_FALLBACK_AFTER_RETRIES", "1")))
        # 模型白名单护栏：仅允许受控的翻译模型，防止客户端绕过 UI 传入高价模型。
        from app.infra.llm_guard import assert_models_allowed
        assert_models_allowed([self.model, *self.model_fallbacks, self.quality_fallback_model], context="translator")
        self.base_url_fallbacks = self._parse_csv_env("OPENAI_BASE_URL_FALLBACKS")
        env_concurrency = int(os.environ.get("OPENAI_CONCURRENCY", concurrency))
        concurrency_cap = int(os.environ.get("EPUB_TRANSLATION_CONCURRENCY_CAP", "4"))
        env_concurrency = min(env_concurrency, max(1, concurrency_cap))
        self.semaphore = asyncio.Semaphore(max(1, env_concurrency))
        self.max_retries = max(1, int(os.environ.get("OPENAI_MAX_RETRIES", "4")))
        self.quality_retries = max(1, int(os.environ.get("EPUB_TRANSLATION_QUALITY_RETRIES", "2")))
        self.timeout_extra_retries = max(0, int(os.environ.get("OPENAI_TIMEOUT_EXTRA_RETRIES", "2")))
        self.request_timeout = float(os.environ.get("OPENAI_REQUEST_TIMEOUT", "90"))
        if temperature is not None:
            self.temperature = float(temperature)
        else:
            self.temperature = float(os.environ.get("OPENAI_TEMPERATURE", "1.1"))
        self._clients: dict[str, AsyncOpenAI] = {}
        self.stats = TranslationStats()
        self.stats.quality_fallback_model = self.quality_fallback_model

    @staticmethod
    def _short_error(exc_or_message: Exception | str | None, limit: int = 160) -> str:
        text = str(exc_or_message or "").replace("\n", " ").strip()
        if len(text) > limit:
            return text[:limit].rstrip() + "..."
        return text

    def _emit_progress(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)

    def _raise_if_cancelled(self) -> None:
        raise_if_cancelled(self.cancel_check)

    def _invalid_translation_reason(self, original_html: str, translated_html: str) -> str | None:
        if not translated_html:
            return "模型返回空译文"
        if self._looks_like_error_response(translated_html):
            return "模型返回错误说明"
        if self._looks_untranslated(original_html, translated_html):
            return "疑似仍为原文"
        if not self._preserves_inline_tags(original_html, translated_html):
            return "HTML 内联标签不匹配"
        return None

    @staticmethod
    def _parse_csv_env(name: str) -> list[str]:
        raw = os.environ.get(name, "")
        values = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
        return list(dict.fromkeys(values))

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        """鉴权/权限类错误（401/403、API Key 无效）不可通过重试恢复，应立即失败。"""
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status in (401, 403):
            return True
        msg = str(exc).lower()
        if "authentication" in msg or "unauthorized" in msg or "permission" in msg:
            return True
        if "api key" in msg and ("invalid" in msg or "incorrect" in msg):
            return True
        return False

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
            return True
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status in (408, 504):
            return True
        msg = str(exc).lower()
        return "timeout" in msg or "timed out" in msg or "read timed out" in msg

    @staticmethod
    def _classify_error(exc_or_message: Exception | str | None) -> str | None:
        if exc_or_message is None:
            return None
        msg = str(exc_or_message).lower()
        if "untranslated" in msg:
            return "untranslated_response"
        if "html tag mismatch" in msg:
            return "html_mismatch"
        if "json" in msg or "parse" in msg:
            return "invalid_json"
        if "timeout" in msg or "timed out" in msg:
            return "timeout"
        if "rate limit" in msg or "429" in msg:
            return "rate_limit"
        if "authentication" in msg or "unauthorized" in msg or "api key" in msg:
            return "auth"
        if "connection" in msg or "connect" in msg:
            return "connection"
        return "model_error"

    def _get_client(self, base_url: str) -> AsyncOpenAI:
        if base_url not in self._clients:
            limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
            http_client = httpx.AsyncClient(timeout=self.request_timeout, limits=limits)
            self._clients[base_url] = AsyncOpenAI(
                api_key=self.api_key,
                base_url=base_url,
                max_retries=0,
                http_client=http_client,
            )
        return self._clients[base_url]

    def _candidate_routes(self, preferred_model: str | None = None) -> list[tuple[str, str]]:
        routes: list[tuple[str, str]] = []
        base_urls = [self.base_url, *self.base_url_fallbacks]
        primary_model = (preferred_model or self.model).strip()
        models = [primary_model] if preferred_model else [self.model, *self.model_fallbacks]
        for base in base_urls:
            routes.append((base, primary_model))
        for model in models[1:]:
            routes.append((self.base_url, model))
        deduped = []
        seen = set()
        for route in routes:
            if route in seen:
                continue
            seen.add(route)
            deduped.append(route)
        return deduped or [(self.base_url, primary_model)]

    def _quality_retry_preferred_model(self, failed_quality_retries: int) -> str | None:
        if self.pro_fallback_after_retries <= 0:
            return None
        if failed_quality_retries < self.pro_fallback_after_retries:
            return None
        if not self.quality_fallback_model or self.quality_fallback_model == self.model:
            return None
        return self.quality_fallback_model

    def _build_system_prompt(self) -> str:
        prompt = f"""你是一位忠实翻译原版书籍的专业译者。目标语言是：{self.target_lang}。
你将收到一个包含多段待翻译内容的 JSON 数组（输入格式为：[{{"id": 0, "html": "..."}}, ...]）。
规则：
1. 忠实翻译 "html" 字段中的文本内容，原文优先，不追求“信达雅”式改写。
2. 不得删减、总结、解释、本土化、审查、弱化或替作者表达；原文中的事实、立场、语气、冒犯性、政治性、宗教性、争议性内容都必须保留并翻译。
3. 不得为了通顺擅自改写逻辑关系、因果关系、否定、程度副词、时间、数量、引号、脚注编号或专有名词。
4. 不确定的专有名词或术语优先按术语表；术语表没有且无法可靠翻译时，可保留原文，不要瞎编。
5. 绝对不能修改、增加或删除任何 HTML 标签及属性（如 id, class, href）。保持标签与对应文字的包裹关系完全一致。若看到类似 [[EPUB_TAG_0_OPEN]] / [[EPUB_TAG_0_CLOSE]] 的占位符，它代表 HTML 标签边界，必须逐字保留；占位符之间的正文仍需翻译。
6. 必须返回一个包含翻译结果的 JSON 对象，格式必须严格为：
{{
  "results": [
    {{"id": 0, "translation": "翻译后的内容"}},
    {{"id": 1, "translation": "..."}}
  ]
}}
7. 返回的 JSON 必须包含输入中的每一个 id，绝对不能遗漏、合并、拆分或重排！"""

        if self.glossary:
            lines = "\n".join(f"  {src} → {dst}" for src, dst in self.glossary.items())
            prompt += (
                "\n\n【全书共享术语对照表】（所有章节统一使用，禁止译名漂移；"
                "遇到以下原文必须严格使用对应译名；同一原文出现多次，译名必须完全一致；"
                "若出现表中词汇的变体或大小写差异，也应尽量使用对应译名）：\n"
                + lines
            )

        return prompt

    @property
    def _glossary_hash(self) -> str:
        """术语库的稳定哈希，用于缓存 key 隔离，防止旧任务的译文污染新任务。"""
        if not self.glossary:
            return ""
        import hashlib
        items = sorted(self.glossary.items())
        s = "|".join(f"{k}={v}" for k, v in items)
        return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]

    @property
    def _cache_lang_key(self) -> str:
        """缓存 key 的语言维度：附带 glossary hash 实现隔离。"""
        gh = self._glossary_hash
        return f"{self.target_lang}@{gh}" if gh else self.target_lang

    def _extract_json_from_response(self, text: str) -> dict:
        text = text.strip()
        # Remove markdown codeblocks if exist
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start:end + 1]
        text = re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(text)

    @staticmethod
    def _looks_like_error_response(text: str) -> bool:
        """识别模型把错误/拒答当作译文返回的常见形态。"""
        if not text or not text.strip():
            return False
        low = text.strip().lower()
        markers = (
            "error:",
            "rate limit",
            "cannot translate",
            "can't translate",
            "unable to process",
            "i'm unable",
            "i am unable",
            "sorry,",
        )
        return any(m in low for m in markers)

    @staticmethod
    def _visible_text(html: str) -> str:
        return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)

    @staticmethod
    def _latin_words(text: str) -> list[str]:
        return re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text or "")

    @staticmethod
    def _latin_char_count(text: str) -> int:
        return sum(1 for ch in text or "" if ch.isascii() and ch.isalpha())

    @staticmethod
    def _cjk_char_count(text: str) -> int:
        return len(re.findall(r"[\u3400-\u9fff]", text or ""))

    def _looks_untranslated(self, source_html: str, translated_html: str) -> bool:
        """识别“看起来成功返回、实际仍是英文原文”的译文。

        只在中文目标语言下启用，避免其它目标语言误判。阈值故意保守，允许 DNA、
        journal title、专名等少量拉丁字符残留，但会拦住整段英文或大段英文夹少量中文。
        """
        if not str(self.target_lang or "").lower().startswith("zh"):
            return False
        source_text = self._visible_text(source_html)
        translated_text = self._visible_text(translated_html)
        if not source_text or not translated_text:
            return False

        source_words = self._latin_words(source_text)
        if len(source_words) < 6:
            return False

        translated_words = self._latin_words(translated_text)
        translated_cjk = self._cjk_char_count(translated_text)
        translated_latin = self._latin_char_count(translated_text)

        normalize = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()
        if normalize(source_text) == normalize(translated_text):
            return True

        # 译文几乎没有中文，且英文词数量接近原文，基本就是漏译。
        if (
            len(translated_words) >= max(6, int(len(source_words) * 0.7))
            and translated_cjk < max(6, int(translated_latin * 0.15))
        ):
            return True

        # 中英混杂但英文主体仍占绝对优势，常见于失败批次只做了专名替换后回写。
        if (
            translated_cjk > 0
            and len(translated_words) >= 12
            and translated_latin > max(120, translated_cjk * 2.5)
        ):
            return True

        return False

    @staticmethod
    def _inline_tag_counter(html: str) -> dict[str, int]:
        soup = BeautifulSoup(html or "", "html.parser")
        return {
            name: len(soup.find_all(name))
            for name in sorted({tag.name for tag in soup.find_all(True) if isinstance(tag, Tag)})
            if name not in BLOCK_TAGS
        }

    def _preserves_inline_tags(self, source_html: str, translated_html: str) -> bool:
        return self._inline_tag_counter(source_html) == self._inline_tag_counter(translated_html)

    @staticmethod
    def _has_dropcap_span(html: str) -> bool:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup.find_all("span"):
            classes = tag.get("class") or []
            class_text = " ".join(classes) if isinstance(classes, list) else str(classes)
            if "big" in class_text.split() and re.fullmatch(r"[A-Za-z]", tag.get_text(strip=True) or ""):
                return True
        return False

    def _should_singleton_batch(self, html: str) -> bool:
        if os.environ.get("EPUB_COMPLEX_INLINE_SINGLETON", "1").lower() in {"0", "false", "no", "off"}:
            return False
        counts = self._inline_tag_counter(html)
        if self._has_dropcap_span(html):
            return True
        if counts.get("sup", 0) > 0:
            return True
        if counts.get("a", 0) >= 2:
            return True
        return sum(counts.values()) >= int(os.environ.get("EPUB_COMPLEX_INLINE_TAG_THRESHOLD", "4") or "4")

    @staticmethod
    def _serialize_open_tag(tag: Tag) -> str:
        attrs = []
        for key, value in tag.attrs.items():
            if value is None or value is False:
                continue
            if value is True:
                attrs.append(f" {key}")
                continue
            if isinstance(value, (list, tuple)):
                value_text = " ".join(str(v) for v in value)
            else:
                value_text = str(value)
            attrs.append(f' {key}="{html_lib.escape(value_text, quote=True)}"')
        return f"<{tag.name}{''.join(attrs)}>"

    def _protect_inline_tags(self, fragment_html: str) -> tuple[str, dict[str, str]]:
        """Replace inline tags with stable markers before sending text to the LLM."""
        soup = BeautifulSoup(fragment_html or "", "html.parser")
        replacements: dict[str, str] = {}
        counter = 0
        for tag in list(soup.find_all(True)):
            if not isinstance(tag, Tag) or tag.name in BLOCK_TAGS:
                continue
            open_marker = f"[[EPUB_TAG_{counter}_OPEN]]"
            close_marker = f"[[EPUB_TAG_{counter}_CLOSE]]"
            single_marker = f"[[EPUB_TAG_{counter}_SELF]]"
            counter += 1
            if tag.name in VOID_TAGS or not tag.contents:
                replacements[single_marker] = str(tag)
                tag.replace_with(NavigableString(single_marker))
                continue
            replacements[open_marker] = self._serialize_open_tag(tag)
            replacements[close_marker] = f"</{tag.name}>"
            tag.insert_before(NavigableString(open_marker))
            tag.insert_after(NavigableString(close_marker))
            tag.unwrap()
        protected = "".join(str(item) for item in soup.contents)
        return protected, replacements

    @staticmethod
    def _restore_inline_tag_markers(translated_html: str, replacements: dict[str, str]) -> str:
        restored = translated_html or ""
        for marker in sorted(replacements, key=len, reverse=True):
            restored = restored.replace(marker, replacements[marker])
        return restored

    @staticmethod
    def _missing_inline_tags(source_html: str, translated_html: str) -> bool:
        source = SemanticsTranslator._inline_tag_counter(source_html)
        translated = SemanticsTranslator._inline_tag_counter(translated_html)
        return any(source.get(name, 0) > translated.get(name, 0) for name in source)

    @staticmethod
    def _source_empty_anchor_fragments(source_html: str, translated_html: str) -> list[str]:
        source = BeautifulSoup(source_html or "", "html.parser")
        translated = BeautifulSoup(translated_html or "", "html.parser")
        translated_ids = {tag.get("id") for tag in translated.find_all(True) if tag.get("id")}
        fragments = []
        for tag in source.find_all("a"):
            if tag.find_parent("sup"):
                continue
            if tag.get_text(strip=True):
                continue
            anchor_id = tag.get("id")
            if anchor_id and anchor_id in translated_ids:
                continue
            if anchor_id or tag.get("name"):
                fragments.append(str(tag))
        return fragments

    @staticmethod
    def _source_sup_fragments(source_html: str, translated_html: str) -> list[str]:
        source = BeautifulSoup(source_html or "", "html.parser")
        translated = BeautifulSoup(translated_html or "", "html.parser")
        if len(translated.find_all("sup")) >= len(source.find_all("sup")):
            return []
        return [str(tag) for tag in source.find_all("sup")]

    def _dropcap_wrapper(self, source_html: str) -> tuple[str, str] | None:
        soup = BeautifulSoup(source_html or "", "html.parser")
        for tag in soup.find_all("span"):
            classes = tag.get("class") or []
            class_text = " ".join(classes) if isinstance(classes, list) else str(classes)
            if "big" in class_text.split() and re.fullmatch(r"[A-Za-z]", tag.get_text(strip=True) or ""):
                return self._serialize_open_tag(tag), f"</{tag.name}>"
        return None

    def _full_inline_wrapper(self, source_html: str) -> tuple[str, str] | None:
        soup = BeautifulSoup(source_html or "", "html.parser")
        contents = [c for c in soup.contents if not (isinstance(c, NavigableString) and not str(c).strip())]
        if len(contents) != 1 or not isinstance(contents[0], Tag):
            return None
        tag = contents[0]
        if tag.name not in {"span", "em", "strong", "i", "b"}:
            return None
        return self._serialize_open_tag(tag), f"</{tag.name}>"

    @staticmethod
    def _wrap_first_text_char(fragment_html: str, open_tag: str, close_tag: str) -> str:
        soup = BeautifulSoup(fragment_html or "", "html.parser")
        for node in list(soup.descendants):
            if not isinstance(node, NavigableString):
                continue
            text = str(node)
            match = re.search(r"\S", text)
            if not match:
                continue
            i = match.start()
            pieces = []
            if text[:i]:
                pieces.append(NavigableString(text[:i]))
            wrapped = BeautifulSoup(
                f"{open_tag}{html_lib.escape(text[i], quote=False)}{close_tag}",
                "html.parser",
            )
            pieces.extend(list(wrapped.contents))
            if text[i + 1:]:
                pieces.append(NavigableString(text[i + 1:]))
            node.replace_with(*pieces)
            return "".join(str(item) for item in soup.contents)
        return fragment_html

    def _repair_inline_tags_if_safe(self, source_html: str, translated_html: str) -> tuple[str, bool]:
        if (
            not translated_html
            or self._looks_like_error_response(translated_html)
            or self._looks_untranslated(source_html, translated_html)
            or self._preserves_inline_tags(source_html, translated_html)
            or not self._missing_inline_tags(source_html, translated_html)
        ):
            return translated_html, False

        repaired = translated_html
        empty_anchors = self._source_empty_anchor_fragments(source_html, repaired)
        if empty_anchors:
            repaired = "".join(empty_anchors) + repaired

        sup_fragments = self._source_sup_fragments(source_html, repaired)
        if sup_fragments:
            repaired = repaired.rstrip() + "".join(sup_fragments)

        if self._has_dropcap_span(source_html) and "span" in self._inline_tag_counter(source_html):
            if self._inline_tag_counter(repaired).get("span", 0) < self._inline_tag_counter(source_html).get("span", 0):
                wrapper = self._dropcap_wrapper(source_html)
                if wrapper:
                    repaired = self._wrap_first_text_char(repaired, wrapper[0], wrapper[1])

        source_counts = self._inline_tag_counter(source_html)
        repaired_counts = self._inline_tag_counter(repaired)
        if source_counts.get("span", 0) == 1 and repaired_counts.get("span", 0) == 0:
            wrapper = self._full_inline_wrapper(source_html)
            if wrapper:
                repaired = f"{wrapper[0]}{repaired}{wrapper[1]}"

        if self._preserves_inline_tags(source_html, repaired):
            self.stats.inline_tag_repairs += 1
            return repaired, True
        return translated_html, False

    @staticmethod
    def _extract_inner_html(html: str) -> str:
        """提取块级标签 inner_html，保持外层属性由 Reduce 回写阶段负责。"""
        soup = BeautifulSoup(html or "", "html.parser")
        block_tag = soup.find()
        if block_tag and block_tag.name in BLOCK_TAGS:
            return "".join(str(c) for c in block_tag.contents).strip()
        return (html or "").strip()

    async def _call_llm_json_batch(
        self,
        payload: list[dict],
        *,
        preferred_model: str | None = None,
    ) -> tuple[dict[int, str], dict]:
        """发送 JSON batch 并返回解析后的 {id: translation} 字典。"""
        self._raise_if_cancelled()
        system_prompt = self._build_system_prompt()
        protected_payload: list[dict] = []
        protected_replacements: dict[int, dict[str, str]] = {}
        for item in payload:
            item_id = int(item["id"])
            protected_html, replacements = self._protect_inline_tags(str(item.get("html", "") or ""))
            protected_item = dict(item)
            protected_item["html"] = protected_html
            protected_payload.append(protected_item)
            protected_replacements[item_id] = replacements
        user_content = json.dumps(protected_payload, ensure_ascii=False)
        last_error = None
        routes = self._candidate_routes(preferred_model)
        max_attempts = max(self.max_retries, len(routes))
        timeout_bonus_used = 0
        response = None
        base_url, model = self.base_url, preferred_model or self.model
        started_call = time.monotonic()
        
        for attempt in range(1, max_attempts + 1):
            self._raise_if_cancelled()
            base_url, model = routes[(attempt - 1) % len(routes)]
            try:
                kwargs = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    "temperature": self.temperature,
                    "timeout": self.request_timeout,
                }
                if os.environ.get("OPENAI_DISABLE_JSON_RESPONSE_FORMAT", "").lower() not in ("1", "true", "yes"):
                    kwargs["response_format"] = {"type": "json_object"}
                try:
                    response = await self._get_client(base_url).chat.completions.create(**kwargs)
                except Exception as response_exc:
                    if "response_format" not in str(response_exc).lower():
                        raise
                    kwargs.pop("response_format", None)
                    response = await self._get_client(base_url).chat.completions.create(**kwargs)
                raw = (response.choices[0].message.content or "").strip()
                try:
                    parsed = self._extract_json_from_response(raw)
                    results_list = parsed.get("results", [])
                    if not isinstance(results_list, list) or len(results_list) != len(payload):
                        raise ValueError(f"JSON 结构不对或数量不匹配: 期望 {len(payload)}, 收到 {len(results_list)}")
                    
                    translations = {}
                    for item in results_list:
                        try:
                            item_id = int(item["id"])
                        except (KeyError, TypeError, ValueError) as id_err:
                            raise ValueError(f"JSON result id invalid: {item!r}") from id_err
                        raw_translation = item.get("translation", "")
                        translations[item_id] = self._restore_inline_tag_markers(
                            raw_translation,
                            protected_replacements.get(item_id, {}),
                        )

                    expected_ids = {item["id"] for item in payload}
                    if set(translations.keys()) != expected_ids:
                        raise ValueError(
                            f"JSON result ids mismatch: expected {sorted(expected_ids)}, got {sorted(translations.keys())}"
                        )
                    
                    if model != self.model or base_url != self.base_url:
                        print(f"⚠️ LLM fallback route succeeded: model={model}, base_url={base_url}")
                    break
                except (json.JSONDecodeError, ValueError) as json_err:
                    last_error = ValueError(f"Failed to parse LLM JSON: {json_err}")
                    self.stats.last_error = str(last_error)
                    if attempt >= max_attempts:
                        raise last_error
                    self.stats.retry_attempts += 1
                    self._emit_progress(
                        f"JSON 解析失败，继续重试 ({attempt}/{max_attempts})：{self._short_error(json_err)}"
                    )
                    await asyncio.sleep(min(2 ** (attempt - 1), 5) + random.uniform(0, 1))
                    continue

            except Exception as exc:
                last_error = exc
                self.stats.last_error = str(exc)
                # 鉴权/权限类错误重试也不会恢复，立即失败，避免无意义重试空耗时间与配额
                if self._is_auth_error(exc):
                    self._emit_progress(
                        f"模型鉴权失败，已停止重试：{self._short_error(exc)}"
                    )
                    raise
                if self._is_timeout_error(exc):
                    self.stats.timeout_errors += 1
                    if timeout_bonus_used < self.timeout_extra_retries:
                        timeout_bonus_used += 1
                        max_attempts += 1
                if "connection" in str(exc).lower() or "timeout" in str(exc).lower():
                    self.stats.connection_errors += 1
                if attempt >= max_attempts:
                    self._emit_progress(
                        f"模型调用失败，已达到最大重试 ({attempt}/{max_attempts})：{self._short_error(exc)}"
                    )
                    raise
                self._emit_progress(
                    f"模型连接波动，继续重试 ({attempt}/{max_attempts})：{self._short_error(exc)}"
                )
                self.stats.retry_attempts += 1
                delay = min(2 ** (attempt - 1), 5) + random.uniform(0, 1)
                await asyncio.sleep(delay)
        else:
            raise last_error

        usage = response.usage if response else None
        prompt_tokens = getattr(usage, "prompt_tokens", None) or 0
        completion_tokens = getattr(usage, "completion_tokens", None) or 0
        if usage:
            self.stats.prompt_tokens += prompt_tokens
            self.stats.completion_tokens += completion_tokens
            self.stats.total_tokens += getattr(usage, "total_tokens", None) or (prompt_tokens + completion_tokens)
        self.stats.api_calls += 1
        latency_ms = int((time.monotonic() - started_call) * 1000)
        self.stats.api_latency_ms_total += latency_ms
        self.stats.api_latency_ms_max = max(self.stats.api_latency_ms_max, latency_ms)
        self.stats.api_latency_samples += 1

        meta = {
            "model": model,
            "base_url": base_url,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "attempts": attempt,
        }
        return (translations, meta)

    _BATCH_MAX_CHARS = 6000

    async def _translate_batch(self, html_chunks: list[str]) -> list[str]:
        payload = [{"id": i, "html": html} for i, html in enumerate(html_chunks)]
        async with self.semaphore:
            translations_map, _ = await self._call_llm_json_batch(payload)
        
        results = []
        for i in range(len(html_chunks)):
            trans = translations_map.get(i, html_chunks[i])
            results.append(trans)
            self.cache.set(html_chunks[i], trans, self._cache_lang_key)
            self.stats.translated_chunks += 1
            self.stats.total_chunks += 1
            
        return results

    async def _translate_html_chunk(self, html_chunk: str) -> str:
        self.stats.total_chunks += 1
        cached = self.cache.get(html_chunk, self._cache_lang_key)
        if cached:
            if (
                not self._looks_like_error_response(cached)
                and not self._looks_untranslated(html_chunk, cached)
                and self._preserves_inline_tags(html_chunk, cached)
            ):
                self.stats.cached_chunks += 1
                return cached
            
        payload = [{"id": 0, "html": html_chunk}]
        async with self.semaphore:
            translations_map, _ = await self._call_llm_json_batch(payload)
        
        translated = translations_map.get(0, html_chunk)
        self.cache.set(html_chunk, translated, self._cache_lang_key)
        self.stats.translated_chunks += 1
        return translated

    async def translate_single_chunk_async(self, html: str) -> "SingleChunkResult":
        """单 chunk 调用（在 V2 Worker 中使用）"""
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text() if html else ""
        if not self._should_translate(text):
            return SingleChunkResult(html, True, None, None, 0, 0, 0, None)
            
        self.stats.total_chunks += 1
        
        # 提取 inner_html 传给大模型，防止破坏外层标签属性
        inner_html = self._extract_inner_html(html)
            
        cached = self.cache.get(inner_html, self._cache_lang_key)
        if cached:
            if (
                not self._looks_like_error_response(cached)
                and not self._looks_untranslated(inner_html, cached)
                and self._preserves_inline_tags(inner_html, cached)
            ):
                self.stats.cached_chunks += 1
                # Return cached inner_html directly. apply_chunk_results will handle it.
                return SingleChunkResult(cached, True, None, None, 0, 0, 0, None)
            
        try:
            t0 = time.monotonic()
            payload = [{"id": 0, "html": inner_html}]
            async with self.semaphore:
                translations_map, meta = await self._call_llm_json_batch(payload)
            translated = translations_map.get(0, "")
            if not translated:
                raise ValueError("LLM response missing translation for chunk")
            if self._looks_like_error_response(translated) or self._looks_untranslated(inner_html, translated):
                async with self.semaphore:
                    translations_map, meta = await self._call_llm_json_batch(payload)
                translated = translations_map.get(0, "")
            if translated and not self._preserves_inline_tags(inner_html, translated):
                repaired, did_repair = self._repair_inline_tags_if_safe(inner_html, translated)
                if did_repair:
                    translated = repaired
                else:
                    async with self.semaphore:
                        translations_map, meta = await self._call_llm_json_batch(payload)
                    translated = translations_map.get(0, "")
                    translated, _ = self._repair_inline_tags_if_safe(inner_html, translated)
            if (
                not translated
                or self._looks_like_error_response(translated)
                or self._looks_untranslated(inner_html, translated)
                or not self._preserves_inline_tags(inner_html, translated)
            ):
                raise ValueError("LLM returned untranslated or invalid content")
            latency_ms = int((time.monotonic() - t0) * 1000)
            self.cache.set(inner_html, translated, self._cache_lang_key)
            self.stats.translated_chunks += 1
            return SingleChunkResult(
                translated, False,
                meta.get("model"), meta.get("base_url"),
                meta.get("prompt_tokens", 0), meta.get("completion_tokens", 0),
                latency_ms, None,
                retry_count=max(0, int(meta.get("attempts") or 1) - 1),
            )
        except Exception as e:
            self.stats.failed_chunks += 1
            self.stats.last_error = str(e)
            return SingleChunkResult(
                html, False, None, None, 0, 0, 0, str(e),
                retry_count=self.max_retries,
                error_type=self._classify_error(e),
            )

    async def translate_many_chunks_async(
        self,
        html_chunks: list[str],
        progress_label: str | None = None,
    ) -> list["SingleChunkResult"]:
        """
        批量翻译多个 chunk，供快速 MapReduce 链路使用。

        与 translate_single_chunk_async 相比，这里会先做缓存过滤，再按字符上限把未命中
        的 chunk 合并成 JSON batch 并发提交，减少请求数，同时仍由全局 semaphore 控制速率。
        返回的 translated_html 是 inner_html，Reduce 阶段会负责回写到原块级标签中。
        """
        results: list[SingleChunkResult | None] = [None] * len(html_chunks)
        uncached: list[tuple[int, str]] = []

        for i, html in enumerate(html_chunks):
            self._raise_if_cancelled()
            soup = BeautifulSoup(html or "", "html.parser")
            text = soup.get_text() if html else ""
            if not self._should_translate(text):
                results[i] = SingleChunkResult(html, True, None, None, 0, 0, 0, None)
                continue

            inner_html = self._extract_inner_html(html)
            self.stats.total_chunks += 1
            cached = self.cache.get(inner_html, self._cache_lang_key)
            if cached:
                if (
                    not self._looks_like_error_response(cached)
                    and not self._looks_untranslated(inner_html, cached)
                    and self._preserves_inline_tags(inner_html, cached)
                ):
                    self.stats.cached_chunks += 1
                    results[i] = SingleChunkResult(cached, True, None, None, 0, 0, 0, None)
                else:
                    uncached.append((i, inner_html))
            else:
                uncached.append((i, inner_html))

        batches: list[list[tuple[int, str]]] = []
        cur_batch: list[tuple[int, str]] = []
        cur_chars = 0
        for idx, html in uncached:
            if self._should_singleton_batch(html):
                if cur_batch:
                    batches.append(cur_batch)
                    cur_batch = []
                    cur_chars = 0
                batches.append([(idx, html)])
                self.stats.complex_chunks += 1
                self.stats.complex_singleton_batches += 1
                continue
            if cur_chars + len(html) > self.batch_max_chars and cur_batch:
                batches.append(cur_batch)
                cur_batch = []
                cur_chars = 0
            cur_batch.append((idx, html))
            cur_chars += len(html)
        if cur_batch:
            batches.append(cur_batch)

        completed_batches = 0
        completed_chunks = 0
        total_batches = len(batches)

        def report_batch_progress(batch_size: int) -> None:
            nonlocal completed_batches, completed_chunks
            completed_batches += 1
            completed_chunks += batch_size
            if self.progress_callback and progress_label and total_batches:
                self.progress_callback(
                    f"{progress_label}：{completed_chunks}/{len(html_chunks)} 段"
                )

        def mark_failed(
            idx: int,
            original_inner: str,
            error: str,
            meta: dict | None = None,
            latency_ms: int = 0,
            retry_count: int = 0,
        ) -> None:
            self.stats.errors += 1
            self.stats.failed_chunks += 1
            self.stats.last_error = error
            self._emit_progress(
                f"段落翻译最终失败，已回写原文：{self._short_error(error)}"
            )
            results[idx] = SingleChunkResult(
                translated_html=original_inner,
                cached=False,
                model=(meta or {}).get("model"),
                base_url=(meta or {}).get("base_url"),
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=latency_ms,
                error=error,
                retry_count=retry_count,
                error_type=self._classify_error(error),
            )

        async def retry_one(idx: int, original_inner: str, reason: str) -> bool:
            self._raise_if_cancelled()
            reason_detail = reason
            last_meta: dict | None = None
            last_latency_ms = 0
            retry_count = 0
            reason_label = {
                "error-like response": "模型返回错误说明",
                "untranslated response": "疑似仍为原文",
                "html tag mismatch": "HTML 内联标签不匹配",
            }.get(reason, reason)
            self._emit_progress(f"段落质检未通过，启动单段补译：{reason_label}")
            for quality_attempt in range(1, self.quality_retries + 1):
                self._raise_if_cancelled()
                t0 = time.monotonic()
                preferred_model = self._quality_retry_preferred_model(quality_attempt - 1)
                try:
                    self.stats.retry_attempts += 1
                    async with self.semaphore:
                        if preferred_model:
                            self.stats.quality_fallback_attempts += 1
                            self._emit_progress(f"单段补译升级模型：{preferred_model}")
                            translations_map, meta = await self._call_llm_json_batch(
                                [{"id": 0, "html": original_inner}],
                                preferred_model=preferred_model,
                            )
                        else:
                            translations_map, meta = await self._call_llm_json_batch([{"id": 0, "html": original_inner}])
                    last_meta = meta
                    retry_count += max(1, int(meta.get("attempts") or 1))
                    translated = translations_map.get(0, "")
                    last_latency_ms = int((time.monotonic() - t0) * 1000)
                except Exception as exc:
                    reason_detail = f"{reason}; retry failed: {exc}"
                    if self._is_auth_error(exc) or quality_attempt >= self.quality_retries:
                        break
                    self._emit_progress(
                        f"单段补译失败，继续重试 ({quality_attempt}/{self.quality_retries})：{self._short_error(exc)}"
                    )
                    await asyncio.sleep(min(quality_attempt, 3) + random.uniform(0, 0.5))
                    continue

                translated, repaired = self._repair_inline_tags_if_safe(original_inner, translated)
                if repaired:
                    self._emit_progress("HTML 标签结构已自动修复，跳过额外补译")
                invalid_reason = self._invalid_translation_reason(original_inner, translated)
                if invalid_reason:
                    reason_detail = f"{reason}; html tag mismatch" if invalid_reason == "HTML 内联标签不匹配" else reason
                    if quality_attempt < self.quality_retries:
                        self._emit_progress(
                            f"单段补译未通过，继续重试 ({quality_attempt}/{self.quality_retries})：{invalid_reason}"
                        )
                        await asyncio.sleep(min(quality_attempt, 3) + random.uniform(0, 0.5))
                        continue
                    break

                self.cache.set(original_inner, translated, self._cache_lang_key)
                self.stats.translated_chunks += 1
                results[idx] = SingleChunkResult(
                    translated_html=translated,
                    cached=False,
                    model=meta.get("model"),
                    base_url=meta.get("base_url"),
                    prompt_tokens=meta.get("prompt_tokens", 0),
                    completion_tokens=meta.get("completion_tokens", 0),
                    latency_ms=last_latency_ms,
                    error=None,
                    retry_count=max(1, retry_count),
                )
                return True

            mark_failed(
                idx,
                original_inner,
                f"{reason_detail}; retry still invalid",
                last_meta,
                last_latency_ms,
                retry_count=max(1, retry_count),
            )
            return False

        async def run_batch(batch: list[tuple[int, str]]) -> None:
            self._raise_if_cancelled()
            payload = [{"id": local_id, "html": html} for local_id, (_, html) in enumerate(batch)]
            t0 = time.monotonic()
            try:
                async with self.semaphore:
                    self._raise_if_cancelled()
                    translations_map, meta = await self._call_llm_json_batch(payload)
            except Exception as exc:
                # 整批 API/网络/JSON 失败时，先拆小重试，避免一个坏 chunk 连累整批。
                self.stats.last_error = str(exc)
                if len(batch) > 1:
                    mid = max(1, len(batch) // 2)
                    self._emit_progress(
                        f"批量翻译失败，拆分重试：{len(batch)} 段 -> {mid}+{len(batch) - mid}，原因：{self._short_error(exc)}"
                    )
                    await asyncio.gather(run_batch(batch[:mid]), run_batch(batch[mid:]))
                else:
                    idx, original_inner = batch[0]
                    self._emit_progress(
                        f"单段翻译失败，无法继续拆分：{self._short_error(exc)}"
                    )
                    mark_failed(idx, original_inner, str(exc), retry_count=self.max_retries)
                    report_batch_progress(1)
                return

            latency_ms = int((time.monotonic() - t0) * 1000)
            count = max(1, len(batch))
            prompt_each = int((meta.get("prompt_tokens", 0) or 0) / count)
            completion_each = int((meta.get("completion_tokens", 0) or 0) / count)

            # 逐块降级：单块被判为错误响应只标记该块失败，不连累同批其它正常译文。
            for local_id, (idx, original_inner) in enumerate(batch):
                self._raise_if_cancelled()
                translated = translations_map.get(local_id, "")
                if self._looks_like_error_response(translated):
                    await retry_one(idx, original_inner, "error-like response")
                    continue
                if not translated or self._looks_untranslated(original_inner, translated):
                    await retry_one(idx, original_inner, "untranslated response")
                    continue
                translated, repaired = self._repair_inline_tags_if_safe(original_inner, translated)
                if repaired:
                    self._emit_progress("HTML 标签结构已自动修复，跳过单段补译")
                if not self._preserves_inline_tags(original_inner, translated):
                    await retry_one(idx, original_inner, "html tag mismatch")
                    continue
                self.cache.set(original_inner, translated, self._cache_lang_key)
                self.stats.translated_chunks += 1
                results[idx] = SingleChunkResult(
                    translated_html=translated,
                    cached=False,
                    model=meta.get("model"),
                    base_url=meta.get("base_url"),
                    prompt_tokens=prompt_each,
                    completion_tokens=completion_each,
                    latency_ms=latency_ms,
                    error=None,
                    retry_count=max(0, int(meta.get("attempts") or 1) - 1),
                )
            report_batch_progress(len(batch))

        if batches:
            self._raise_if_cancelled()
            await asyncio.gather(*(run_batch(batch) for batch in batches))

        self._raise_if_cancelled()
        missing = [i for i, r in enumerate(results) if r is None]
        if missing:
            error = f"chunk not processed: {len(missing)} 段"
            self.stats.errors += len(missing)
            self.stats.failed_chunks += len(missing)
            self.stats.last_error = error
            self._emit_progress(f"翻译结果缺失：{len(missing)} 个段落未处理，已回写原文")

        return [
            r if r is not None else SingleChunkResult(html_chunks[i], False, None, None, 0, 0, 0, "chunk not processed")
            for i, r in enumerate(results)
        ]

    def _should_translate(self, text: str) -> bool:
        if not text.strip():
            return False
        if not re.search('[a-zA-Z]', text):
            return False
        return True

    async def process_async(self, content: bytes, item_type: int) -> bytes:
        if item_type == 9:
            text = content.decode('utf-8', errors='ignore')
            
            stripped = re.sub(r'<[^>]+>', '', text)
            if not self._should_translate(stripped):
                return content

            soup = BeautifulSoup(text, 'html.parser')
            blocks = soup.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote'])
            
            translatable_blocks = []
            inner_htmls = []
            
            for block in blocks:
                has_block_child = block.find(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote'])
                if has_block_child:
                    continue
                # 不取整个 block，而是抽取其 inner_html
                inner_html = "".join(str(c) for c in block.contents).strip()
                if not self._should_translate(inner_html):
                    continue
                translatable_blocks.append(block)
                inner_htmls.append(inner_html)

            if translatable_blocks:
                total_blocks = len(translatable_blocks)
                cached_results: dict[int, str] = {}
                uncached: list[tuple[int, str]] = []
                for i, h_inner in enumerate(inner_htmls):
                    c = self.cache.get(h_inner, self._cache_lang_key)
                    if c:
                        cached_results[i] = c
                        self.stats.total_chunks += 1
                        self.stats.cached_chunks += 1
                    else:
                        uncached.append((i, h_inner))

                if self.progress_callback and cached_results:
                    self.progress_callback(f"缓存命中 {len(cached_results)}/{total_blocks} 句")

                uncached_results: dict[int, str] = {}

                # 先把未缓存段落切成多个批次（按字符上限），再并发提交。
                # 并发度由 self.semaphore(OPENAI_CONCURRENCY，默认 5) 在 _translate_batch
                # 内部约束，既能把吞吐拉满，又不会击穿内存/速率限制。
                batches: list[list[tuple[int, str]]] = []
                cur_batch: list[tuple[int, str]] = []
                cur_chars = 0
                for idx, html in uncached:
                    if cur_chars + len(html) > self.batch_max_chars and cur_batch:
                        batches.append(cur_batch)
                        cur_batch = []
                        cur_chars = 0
                    cur_batch.append((idx, html))
                    cur_chars += len(html)
                if cur_batch:
                    batches.append(cur_batch)

                async def run_batch(batch: list[tuple[int, str]]):
                    indices = [idx for idx, _ in batch]
                    htmls = [h for _, h in batch]
                    try:
                        translated_list = await self._translate_batch(htmls)
                        for idx, t in zip(indices, translated_list):
                            uncached_results[idx] = t
                    except Exception as e:
                        self._emit_progress(
                            f"批量翻译失败，已回写原文：{self._short_error(e)}"
                        )
                        for idx, h in zip(indices, htmls):
                            self.stats.errors += 1
                            self.stats.failed_chunks += 1
                            self.stats.last_error = str(e)
                            uncached_results[idx] = h  # fallback to original

                    if self.progress_callback:
                        done = len(cached_results) + len(uncached_results)
                        self.progress_callback(f"{done}/{total_blocks} 句")

                if batches:
                    await asyncio.gather(*(run_batch(b) for b in batches))

                for i in range(total_blocks):
                    block = translatable_blocks[i]
                    original_inner = inner_htmls[i]
                    translated_inner = cached_results.get(i) or uncached_results.get(i) or original_inner

                    if self.bilingual:
                        # 安全双语注入，不破坏 Box Model 结构
                        new_content = BeautifulSoup(
                            f'<span class="epub-original">{original_inner}</span><br/><span class="epub-translated">{translated_inner}</span>', 
                            'html.parser'
                        )
                        block.clear()
                        for child in new_content.contents:
                            block.append(child)
                    else:
                        new_content = BeautifulSoup(translated_inner, 'html.parser')
                        block.clear()
                        for child in new_content.contents:
                            block.append(child)

            return str(soup).encode('utf-8')
            
        return content

    def process(self, content: bytes, item_type: int) -> bytes:
        if item_type == 9:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            if loop.is_running():
                import nest_asyncio
                nest_asyncio.apply()
            return loop.run_until_complete(self.process_async(content, item_type))
        return content
