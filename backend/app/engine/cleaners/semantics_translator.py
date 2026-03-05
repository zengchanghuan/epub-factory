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
        
        # 默认尝试从环境变量读取，支持更换 Base URL (如 DeepSeek: https://api.deepseek.com)
        self.client = AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        )
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.semaphore = asyncio.Semaphore(concurrency)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _call_llm(self, html_chunk: str) -> str:
        system_prompt = f"""
你是一位顶级的书籍翻译专家。目标语言是：{self.target_lang}。
用户将给你一段包含 HTML 标签的文本。
规则：
1. 翻译文本内容，使其符合目标语言的母语表达习惯，信达雅。
2. 绝对不能修改、增加或删除任何 HTML 标签及属性（如 id, class, href）。
3. 保持标签与对应文字的包裹关系完全一致。
4. 只输出翻译后的 HTML 字符串，不要输出任何解释，不要包含 markdown 代码块外壳（如 ```html ）。
"""
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": html_chunk}
            ],
            temperature=0.3
        )
        result = response.choices[0].message.content.strip()
        # 清理可能存在的 markdown 外壳
        result = re.sub(r"^```html\s*", "", result)
        result = re.sub(r"\s*```$", "", result)
        return result

    async def _translate_html_chunk(self, html_chunk: str) -> str:
        # 1. 查缓存
        cached = self.cache.get(html_chunk, self.target_lang)
        if cached:
            return cached
            
        # 2. 控制并发调用 LLM
        async with self.semaphore:
            print(f"🔄 Translating chunk... (Model: {self.model})")
            translated = await self._call_llm(html_chunk)
            
        # 3. 写入缓存
        self.cache.set(html_chunk, translated, self.target_lang)
        return translated

    def _should_translate(self, text: str) -> bool:
        if not text.strip():
            return False
        # 如果是纯数字或符号，跳过（英文环境通常包含字母）
        if not re.search('[a-zA-Z]', text):
            return False
        return True

    async def process_async(self, content: bytes, item_type: int) -> bytes:
        if item_type == 9: # HTML 文件
            soup = BeautifulSoup(content, 'xml') # 用 xml 解析器防止自动补全 html/body
            
            # 获取所有可能包含文本的块级标签
            blocks = soup.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote'])
            
            tasks = []
            block_refs = []
            
            for block in blocks:
                # 避免重复翻译：只翻译内部没有块级子元素的“叶子”块级标签
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
                        # 解析返回的 HTML 并替换原节点
                        new_soup = BeautifulSoup(translated_html, 'xml')
                        new_tag = new_soup.find()
                        if new_tag:
                            block.replace_with(new_tag)
                        else:
                            # 降级：如果大模型返回的不是标签，当做纯文本塞回去
                            block.string = translated_html
                    except Exception as e:
                        print(f"❌ Error replacing tag: {e}")
                        
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