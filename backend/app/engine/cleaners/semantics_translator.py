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
            
            # 策略：遇到 SVG 需要非常小心，不应把它当成普通的包裹文本的块级标签来翻译。
            # 大多数排版引擎在 <svg> 里用 <text> 或者 <image>。我们直接在顶层把 SVG 跳过。
            # （也可以用 BeautifulSoup 的 extract 或 decompose 暂时移除，但在我们的流式处理中跳过最稳妥）
            for svg_tag in soup.find_all('svg'):
                # 给一个特殊的标记，防止后面被搜到
                svg_tag['data-no-translate'] = "true"

            # 【修复图片被破坏的核心问题】
            # 有些电子书把 <img> 标签直接丢在 <div> 里，没有 <p> 包裹。为了安全起见，
            # 给所有的 <img>, <image> 父级元素也打上不翻译的标签。
            for img_tag in soup.find_all(['img', 'image']):
                parent = img_tag.find_parent()
                if parent:
                    parent['data-no-translate'] = "true"

            # 获取所有可能包含文本的块级标签
            # 同样跳过带有 data-no-translate 的节点
            blocks = soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote'])
            
            tasks = []
            block_refs = []
            
            for block in blocks:
                # 检查自身或父级是否被标记为不翻译
                if block.get('data-no-translate') or block.find_parent(attrs={"data-no-translate": "true"}):
                    continue

                # 避免重复翻译：只翻译内部没有块级子元素的“叶子”块级标签
                has_block_child = block.find(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote'])
                if has_block_child:
                    continue
                    
                # 【优化：保护图片】如果块内包含 <img> 或 <svg> 或 <image>，跳过翻译
                if block.find(['img', 'svg', 'image']):
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

            # 清理我们打上去的临时标记
            for tag in soup.find_all(attrs={"data-no-translate": "true"}):
                del tag['data-no-translate']
                        
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