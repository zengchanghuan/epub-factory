import asyncio
import re
import os
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from ..translation_cache import TranslationCache

class SemanticsTranslator:
    def __init__(self, target_lang="zh-CN", concurrency=5):
        self.target_lang = target_lang
        self.cache = TranslationCache()
        
        self.client = AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        )
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.semaphore = asyncio.Semaphore(concurrency)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _call_llm(self, html_chunk: str) -> str:
        system_prompt = f"""你是一位顶级的书籍翻译专家。目标语言是：{self.target_lang}。
用户将给你一段包含 HTML 标签的文本。
规则：
1. 翻译文本内容，使其符合目标语言的母语表达习惯，信达雅。
2. 绝对不能修改、增加或删除任何 HTML 标签及属性（如 id, class, href）。
3. 保持标签与对应文字的包裹关系完全一致。
4. 只输出翻译后的 HTML 字符串，不要输出任何解释，不要包含 markdown 代码块外壳。"""
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": html_chunk}
            ],
            temperature=0.3
        )
        result = response.choices[0].message.content.strip()
        result = re.sub(r"^```html\s*", "", result)
        result = re.sub(r"\s*```$", "", result)
        return result

    async def _translate_html_chunk(self, html_chunk: str) -> str:
        cached = self.cache.get(html_chunk, self.target_lang)
        if cached:
            return cached
        async with self.semaphore:
            print(f"🔄 Translating chunk... (Model: {self.model})")
            translated = await self._call_llm(html_chunk)
        self.cache.set(html_chunk, translated, self.target_lang)
        return translated

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
            
            tasks = []
            block_refs = []
            
            for block in blocks:
                has_block_child = block.find(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote'])
                if has_block_child:
                    continue
                original_html = str(block)
                if not self._should_translate(block.get_text()):
                    continue
                tasks.append(self._translate_html_chunk(original_html))
                block_refs.append(block)

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for block, translated_html in zip(block_refs, results):
                    if isinstance(translated_html, Exception):
                        print(f"❌ Translation error: {translated_html}")
                        continue
                    try:
                        new_soup = BeautifulSoup(translated_html, 'html.parser')
                        new_tag = new_soup.find()
                        if new_tag:
                            block.replace_with(new_tag)
                        else:
                            block.string = translated_html
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