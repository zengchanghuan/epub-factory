import asyncio
import random
import re
import os
import time
from dataclasses import dataclass, field
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
import httpx
from ..translation_cache import TranslationCache


# DeepSeek 定价 (每百万 token)
PRICING = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}


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
    last_error: str = ""

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
            "cost_usd": self.estimate_cost(model),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "last_error": self.last_error,
            "all_failed": self.total_chunks > 0 and (self.translated_chunks + self.cached_chunks) == 0 and self.failed_chunks > 0,
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


class SemanticsTranslator:
    def __init__(self, target_lang="zh-CN", concurrency=5, bilingual=False,
                 glossary: dict | None = None):
        self.target_lang = target_lang
        self.bilingual = bilingual  # True = 原文 + 译文并排
        self.glossary: dict[str, str] = glossary or {}  # {原文术语: 目标语言术语}
        self.cache = TranslationCache()
        self.progress_callback = None

        self.api_key = os.environ.get("OPENAI_API_KEY", "dummy")
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.model_fallbacks = self._parse_csv_env("OPENAI_MODEL_FALLBACKS")
        self.base_url_fallbacks = self._parse_csv_env("OPENAI_BASE_URL_FALLBACKS")
        env_concurrency = int(os.environ.get("OPENAI_CONCURRENCY", concurrency))
        self.semaphore = asyncio.Semaphore(max(1, env_concurrency))
        self.max_retries = max(1, int(os.environ.get("OPENAI_MAX_RETRIES", "2")))
        self.request_timeout = float(os.environ.get("OPENAI_REQUEST_TIMEOUT", "25"))
        self._clients: dict[str, AsyncOpenAI] = {}
        self.stats = TranslationStats()

    @staticmethod
    def _parse_csv_env(name: str) -> list[str]:
        raw = os.environ.get(name, "")
        values = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
        return list(dict.fromkeys(values))

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

    def _candidate_routes(self) -> list[tuple[str, str]]:
        routes: list[tuple[str, str]] = []
        base_urls = [self.base_url, *self.base_url_fallbacks]
        models = [self.model, *self.model_fallbacks]
        for base in base_urls:
            routes.append((base, self.model))
        for model in models[1:]:
            routes.append((self.base_url, model))
        deduped = []
        seen = set()
        for route in routes:
            if route in seen:
                continue
            seen.add(route)
            deduped.append(route)
        return deduped or [(self.base_url, self.model)]

    def _build_system_prompt(self) -> str:
        """构建 System Prompt，若有术语表则注入为上下文（RAG）"""
        prompt = f"""你是一位顶级的书籍翻译专家。目标语言是：{self.target_lang}。
用户将给你一段或多段包含 HTML 标签的文本。
规则：
1. 翻译文本内容，使其符合目标语言的母语表达习惯，信达雅。
2. 绝对不能修改、增加或删除任何 HTML 标签及属性（如 id, class, href）。
3. 保持标签与对应文字的包裹关系完全一致。
4. 只输出翻译后的 HTML 字符串，不要输出任何解释，不要包含 markdown 代码块外壳。
5. 如果输入中包含 <!-- CHUNK_SEP --> 分隔符，则每段独立翻译，输出中必须保留同样数量的 <!-- CHUNK_SEP --> 分隔符将各段翻译结果分开。"""

        if self.glossary:
            lines = "\n".join(f"  {src} → {dst}" for src, dst in self.glossary.items())
            prompt += f"\n\n术语对照表（必须严格遵守，遇到以下原文术语时使用指定译文）：\n{lines}"

        return prompt

    @staticmethod
    def _looks_like_html(text: str) -> bool:
        """结果校验：至少包含标签形结构，避免纯错误文案被当作译文。"""
        if not text or not text.strip():
            return False
        s = text.strip()
        return "<" in s and ">" in s

    @staticmethod
    def _looks_like_error_response(text: str) -> bool:
        """结果校验：常见模型错误/拒绝回复前缀，视为无效需重试。"""
        lower = (text or "").strip().lower()[:80]
        prefixes = ("error", "sorry", "i cannot", "i'm unable", "i am unable", "api", "rate limit", "invalid")
        return any(lower.startswith(p) for p in prefixes)

    async def _call_llm(self, html_chunk: str) -> tuple[str, dict]:
        """返回 (result_text, meta)；含 base_url/model fallback、指数退避+jitter、结果校验。"""
        system_prompt = self._build_system_prompt()
        last_error = None
        routes = self._candidate_routes()
        max_attempts = max(self.max_retries, len(routes))
        response = None
        base_url, model = self.base_url, self.model
        for attempt in range(1, max_attempts + 1):
            base_url, model = routes[(attempt - 1) % len(routes)]
            try:
                response = await self._get_client(base_url).chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": html_chunk}
                    ],
                    temperature=0.3,
                    timeout=self.request_timeout,
                )
                raw = (response.choices[0].message.content or "").strip()
                result = re.sub(r"^```html\s*", "", raw)
                result = re.sub(r"\s*```$", "", result)
                if not self._looks_like_html(result) or self._looks_like_error_response(result):
                    last_error = ValueError("Invalid response: not HTML or error-like")
                    self.stats.last_error = str(last_error)
                    if attempt >= max_attempts:
                        raise last_error
                    if self.progress_callback:
                        self.progress_callback(f"译文校验未通过，重试 ({attempt}/{max_attempts})")
                    await asyncio.sleep(min(2 ** (attempt - 1), 5) + random.uniform(0, 1))
                    continue
                if model != self.model or base_url != self.base_url:
                    print(f"⚠️ LLM fallback route succeeded: model={model}, base_url={base_url}")
                break
            except Exception as exc:
                last_error = exc
                self.stats.last_error = str(exc)
                if "connection" in str(exc).lower() or "timeout" in str(exc).lower():
                    self.stats.connection_errors += 1
                if self.progress_callback:
                    self.progress_callback(f"模型连接波动，正在重试 ({attempt}/{max_attempts})")
                if attempt >= max_attempts:
                    raise
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

        result = (response.choices[0].message.content or "").strip()
        result = re.sub(r"^```html\s*", "", result)
        result = re.sub(r"\s*```$", "", result)
        meta = {"model": model, "base_url": base_url, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
        return (result, meta)

    _BATCH_SEPARATOR = "\n<!-- CHUNK_SEP -->\n"
    _BATCH_MAX_CHARS = 3000

    async def _translate_html_chunk(self, html_chunk: str) -> str:
        self.stats.total_chunks += 1
        cached = self.cache.get(html_chunk, self.target_lang)
        if cached:
            self.stats.cached_chunks += 1
            return cached
        async with self.semaphore:
            translated, _ = await self._call_llm(html_chunk)
        self.cache.set(html_chunk, translated, self.target_lang)
        self.stats.translated_chunks += 1
        return translated

    async def _translate_batch(self, chunks: list[str]) -> list[str]:
        """将多个短 HTML 块合并为一次 LLM 调用，用分隔符拆分结果，大幅减少调用次数。"""
        merged = self._BATCH_SEPARATOR.join(chunks)
        async with self.semaphore:
            translated, _ = await self._call_llm(merged)
        parts = translated.split("<!-- CHUNK_SEP -->")
        parts = [p.strip() for p in parts]
        if len(parts) != len(chunks):
            results = []
            for c in chunks:
                r = await self._translate_html_chunk(c)
                results.append(r)
            return results
        for orig, trans in zip(chunks, parts):
            self.cache.set(orig, trans, self.target_lang)
            self.stats.translated_chunks += 1
            self.stats.total_chunks += 1
        return parts

    async def translate_single_chunk_async(self, html: str) -> "SingleChunkResult":
        """
        翻译单段 HTML chunk，供章节级任务按 chunk 并发调用与持久化。
        :return: SingleChunkResult(translated, cached, model, base_url, prompt_tokens, completion_tokens, latency_ms, error)
        """
        text = BeautifulSoup(html, "html.parser").get_text() if html else ""
        if not self._should_translate(text):
            return SingleChunkResult(html, True, None, None, 0, 0, 0, None)
        self.stats.total_chunks += 1
        cached = self.cache.get(html, self.target_lang)
        if cached:
            self.stats.cached_chunks += 1
            return SingleChunkResult(cached, True, None, None, 0, 0, 0, None)
        try:
            t0 = time.monotonic()
            async with self.semaphore:
                translated, meta = await self._call_llm(html)
            latency_ms = int((time.monotonic() - t0) * 1000)
            self.cache.set(html, translated, self.target_lang)
            self.stats.translated_chunks += 1
            return SingleChunkResult(
                translated, False,
                meta.get("model"), meta.get("base_url"),
                meta.get("prompt_tokens", 0), meta.get("completion_tokens", 0),
                latency_ms, None,
            )
        except Exception as e:
            self.stats.failed_chunks += 1
            self.stats.last_error = str(e)
            return SingleChunkResult(html, False, None, None, 0, 0, 0, str(e))

    def _should_translate(self, text: str) -> bool:
        if not text.strip():
            return False
        if not re.search('[a-zA-Z]', text):
            return False
        return True

    async def process_async(self, content: bytes, item_type: int) -> bytes:
        if item_type == 9:
            text = content.decode('utf-8', errors='ignore')
            
            # 【快速判断】如果整个页面没有可翻译的英文文本，直接原样返回
            # 避免 BeautifulSoup 解析时破坏 SVG 大小写敏感的属性
            stripped = re.sub(r'<[^>]+>', '', text)
            if not self._should_translate(stripped):
                return content

            # 【纯字符串占位符法】在原始字符串层面用正则把图片/SVG 替换为占位符
            # 绝不经过 BeautifulSoup 的 DOM 操作，100% 保证图片代码不被污染
            placeholders = {}
            placeholder_idx = 0
            
            def make_placeholder(match):
                nonlocal placeholder_idx
                pid = f"IMG_PH_{placeholder_idx}"
                placeholder_idx += 1
                placeholders[pid] = match.group(0)
                return f'<span id="{pid}">{pid}</span>'

            # 替换 <svg>...</svg> 整块（贪心匹配到最近的 </svg>）
            text = re.sub(r'<svg[\s\S]*?</svg>', make_placeholder, text, flags=re.IGNORECASE)
            # 替换自闭合 <img ... /> 或 <img ...>
            text = re.sub(r'<img\b[^>]*/?\s*>', make_placeholder, text, flags=re.IGNORECASE)
            # 替换独立的 <image ... /> (SVG 外的残余)
            text = re.sub(r'<image\b[^>]*/?\s*>', make_placeholder, text, flags=re.IGNORECASE)

            # 现在 text 中只剩纯文本和简单 HTML 标签，安全地交给 BeautifulSoup
            soup = BeautifulSoup(text, 'html.parser')

            blocks = soup.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote'])
            
            translatable_blocks = []
            translatable_htmls = []
            
            for block in blocks:
                has_block_child = block.find(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote'])
                if has_block_child:
                    continue
                original_html = str(block)
                if not self._should_translate(block.get_text()):
                    continue
                translatable_blocks.append(block)
                translatable_htmls.append(original_html)

            if translatable_blocks:
                total_blocks = len(translatable_blocks)
                cached_before = self.stats.cached_chunks

                cached_results: dict[int, str] = {}
                uncached: list[tuple[int, str]] = []
                for i, html in enumerate(translatable_htmls):
                    c = self.cache.get(html, self.target_lang)
                    if c:
                        cached_results[i] = c
                        self.stats.total_chunks += 1
                        self.stats.cached_chunks += 1
                    else:
                        uncached.append((i, html))

                if self.progress_callback and cached_results:
                    self.progress_callback(f"缓存命中 {len(cached_results)}/{total_blocks} 段落")

                uncached_results: dict[int, str] = {}
                batch: list[tuple[int, str]] = []
                batch_chars = 0

                async def flush_batch():
                    nonlocal batch, batch_chars
                    if not batch:
                        return
                    indices = [idx for idx, _ in batch]
                    htmls = [h for _, h in batch]
                    if len(htmls) == 1:
                        translated = await self._translate_html_chunk(htmls[0])
                        uncached_results[indices[0]] = translated
                    else:
                        translated_list = await self._translate_batch(htmls)
                        for idx, t in zip(indices, translated_list):
                            uncached_results[idx] = t
                    if self.progress_callback:
                        done = len(cached_results) + len(uncached_results)
                        self.progress_callback(f"{done}/{total_blocks} 段落")
                    batch = []
                    batch_chars = 0

                for idx, html in uncached:
                    if batch_chars + len(html) > self._BATCH_MAX_CHARS and batch:
                        await flush_batch()
                    batch.append((idx, html))
                    batch_chars += len(html)
                await flush_batch()

                results = []
                for i in range(total_blocks):
                    if i in cached_results:
                        results.append(cached_results[i])
                    elif i in uncached_results:
                        results.append(uncached_results[i])
                    else:
                        results.append(translatable_htmls[i])
                for block, translated_html in zip(translatable_blocks, results):
                    if isinstance(translated_html, Exception):
                        self.stats.errors += 1
                        self.stats.failed_chunks += 1
                        self.stats.last_error = str(translated_html)
                        print(f"❌ Translation error: {translated_html}")
                        continue
                    try:
                        new_soup = BeautifulSoup(translated_html, 'html.parser')
                        new_tag = new_soup.find()
                        if not new_tag:
                            block.string = translated_html
                            continue

                        if self.bilingual:
                            block["class"] = block.get("class", []) + ["epub-original"]
                            new_tag["class"] = new_tag.get("class", []) + ["epub-translated"]
                            block.insert_after(new_tag)
                        else:
                            block.replace_with(new_tag)
                    except Exception as e:
                        print(f"❌ Error replacing tag: {e}")

            result_text = str(soup)
            
            # 纯字符串替换：把占位符换回原始的图片/SVG HTML（绝对安全）
            for pid, original_html in placeholders.items():
                result_text = result_text.replace(f'<span id="{pid}">{pid}</span>', original_html)
                        
            return result_text.encode('utf-8')
            
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