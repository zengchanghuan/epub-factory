import asyncio
import json
import random
import re
import os
import time
from dataclasses import dataclass, field
from bs4 import BeautifulSoup, Tag, NavigableString
from openai import AsyncOpenAI
import httpx
from ..translation_cache import TranslationCache


# 定价：每百万 token 美元（来源：DeepSeek / OpenAI 公开价格，可随官网更新）
PRICING = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-v3": {"input": 0.27, "output": 1.10},
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
                 glossary: dict | None = None, temperature: float | None = None):
        self.target_lang = target_lang
        self.bilingual = bilingual
        self.glossary: dict[str, str] = glossary or {}
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
        self.request_timeout = float(os.environ.get("OPENAI_REQUEST_TIMEOUT", "45"))  # increased for JSON batches
        if temperature is not None:
            self.temperature = float(temperature)
        else:
            self.temperature = float(os.environ.get("OPENAI_TEMPERATURE", "1.1"))
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
        prompt = f"""你是一位顶级的书籍翻译专家。目标语言是：{self.target_lang}。
你将收到一个包含多段待翻译内容的 JSON 数组（输入格式为：[{{"id": 0, "html": "..."}}, ...]）。
规则：
1. 翻译 "html" 字段中的文本内容，使其符合目标语言的母语表达习惯，信达雅。
2. 绝对不能修改、增加或删除任何 HTML 标签及属性（如 id, class, href）。保持标签与对应文字的包裹关系完全一致。
3. 必须返回一个包含翻译结果的 JSON 对象，格式必须严格为：
{{
  "results": [
    {{"id": 0, "translation": "翻译后的内容"}},
    {{"id": 1, "translation": "..."}}
  ]
}}
4. 返回的 JSON 必须包含输入中的每一个 id，绝对不能遗漏或合并！"""

        if self.glossary:
            lines = "\n".join(f"  {src} → {dst}" for src, dst in self.glossary.items())
            prompt += f"\n\n术语对照表（必须严格遵守，遇到以下原文术语时使用指定译文）：\n{lines}"

        return prompt

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
        return json.loads(text)

    async def _call_llm_json_batch(self, payload: list[dict]) -> tuple[dict[int, str], dict]:
        """发送 JSON batch 并返回解析后的 {id: translation} 字典。"""
        system_prompt = self._build_system_prompt()
        user_content = json.dumps(payload, ensure_ascii=False)
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
                        {"role": "user", "content": user_content}
                    ],
                    temperature=self.temperature,
                    response_format={"type": "json_object"} if "gpt" in model.lower() else None,
                    timeout=self.request_timeout,
                )
                raw = (response.choices[0].message.content or "").strip()
                try:
                    parsed = self._extract_json_from_response(raw)
                    results_list = parsed.get("results", [])
                    if not isinstance(results_list, list) or len(results_list) != len(payload):
                        raise ValueError(f"JSON 结构不对或数量不匹配: 期望 {len(payload)}, 收到 {len(results_list)}")
                    
                    translations = {}
                    for item in results_list:
                        translations[item["id"]] = item.get("translation", "")
                    
                    if model != self.model or base_url != self.base_url:
                        print(f"⚠️ LLM fallback route succeeded: model={model}, base_url={base_url}")
                    break
                except (json.JSONDecodeError, ValueError) as json_err:
                    last_error = ValueError(f"Failed to parse LLM JSON: {json_err}")
                    self.stats.last_error = str(last_error)
                    if attempt >= max_attempts:
                        raise last_error
                    if self.progress_callback:
                        self.progress_callback(f"JSON 解析失败，重试 ({attempt}/{max_attempts})")
                    await asyncio.sleep(min(2 ** (attempt - 1), 5) + random.uniform(0, 1))
                    continue

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

        meta = {"model": model, "base_url": base_url, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
        return (translations, meta)

    _BATCH_MAX_CHARS = 2500

    async def _translate_batch(self, html_chunks: list[str]) -> list[str]:
        payload = [{"id": i, "html": html} for i, html in enumerate(html_chunks)]
        async with self.semaphore:
            translations_map, _ = await self._call_llm_json_batch(payload)
        
        results = []
        for i in range(len(html_chunks)):
            trans = translations_map.get(i, html_chunks[i])
            results.append(trans)
            self.cache.set(html_chunks[i], trans, self.target_lang)
            self.stats.translated_chunks += 1
            self.stats.total_chunks += 1
            
        return results

    async def _translate_html_chunk(self, html_chunk: str) -> str:
        self.stats.total_chunks += 1
        cached = self.cache.get(html_chunk, self.target_lang)
        if cached:
            self.stats.cached_chunks += 1
            return cached
            
        payload = [{"id": 0, "html": html_chunk}]
        async with self.semaphore:
            translations_map, _ = await self._call_llm_json_batch(payload)
        
        translated = translations_map.get(0, html_chunk)
        self.cache.set(html_chunk, translated, self.target_lang)
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
        block_tag = soup.find()
        if block_tag and block_tag.name in ['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote']:
            inner_html = "".join(str(c) for c in block_tag.contents).strip()
        else:
            inner_html = html.strip()
            
        cached = self.cache.get(inner_html, self.target_lang)
        if cached:
            self.stats.cached_chunks += 1
            # Return cached inner_html directly. apply_chunk_results will handle it.
            return SingleChunkResult(cached, True, None, None, 0, 0, 0, None)
            
        try:
            t0 = time.monotonic()
            payload = [{"id": 0, "html": inner_html}]
            async with self.semaphore:
                translations_map, meta = await self._call_llm_json_batch(payload)
            translated = translations_map.get(0, inner_html)
            latency_ms = int((time.monotonic() - t0) * 1000)
            self.cache.set(inner_html, translated, self.target_lang)
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
                    c = self.cache.get(h_inner, self.target_lang)
                    if c:
                        cached_results[i] = c
                        self.stats.total_chunks += 1
                        self.stats.cached_chunks += 1
                    else:
                        uncached.append((i, h_inner))

                if self.progress_callback and cached_results:
                    self.progress_callback(f"缓存命中 {len(cached_results)}/{total_blocks} 句")

                uncached_results: dict[int, str] = {}
                batch: list[tuple[int, str]] = []
                batch_chars = 0

                async def flush_batch():
                    nonlocal batch, batch_chars
                    if not batch:
                        return
                    indices = [idx for idx, _ in batch]
                    htmls = [h for _, h in batch]
                    try:
                        translated_list = await self._translate_batch(htmls)
                        for idx, t in zip(indices, translated_list):
                            uncached_results[idx] = t
                    except Exception as e:
                        print(f"❌ Batch translation failed: {e}")
                        for idx, h in zip(indices, htmls):
                            self.stats.errors += 1
                            self.stats.failed_chunks += 1
                            uncached_results[idx] = h  # fallback to original
                    
                    if self.progress_callback:
                        done = len(cached_results) + len(uncached_results)
                        self.progress_callback(f"{done}/{total_blocks} 句")
                    batch = []
                    batch_chars = 0

                for idx, html in uncached:
                    if batch_chars + len(html) > self._BATCH_MAX_CHARS and batch:
                        await flush_batch()
                    batch.append((idx, html))
                    batch_chars += len(html)
                await flush_batch()

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
