import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

from .unpacker import EpubUnpacker
from .packager import EpubPackager
from .cleaners.css_sanitizer import CssSanitizer
from .cleaners.cjk_normalizer import CjkNormalizer
from .cleaners.semantics_translator import SemanticsTranslator

class ExtremeCompiler:
    def __init__(self, input_path: str, output_path: str, output_mode: str = "simplified", enable_translation: bool = False, target_lang: str = "zh-CN"):
        self.input_path = input_path
        self.output_path = output_path
        self.book = None
        self.output_mode = output_mode
        self.enable_translation = enable_translation
        
        # 注册流水线中的清洗器
        self.cleaners = [
            CjkNormalizer(output_mode=self.output_mode),  # 先处理排版方向和简繁
            CssSanitizer()                                # 再进行 CSS 暴力清洗
        ]
        
        if self.enable_translation:
            self.cleaners.append(SemanticsTranslator(target_lang=target_lang))

    def run(self) -> bool:
        print(f"🚀 [Start] Processing: {self.input_path}")
        
        # 1. 解包与读取 (Unpack)
        unpacker = EpubUnpacker(self.input_path)
        self.book = unpacker.load_book()
        
        if not self.book:
            print("❌ [Error] Failed to load EPUB.")
            return False

        # 强制将 spine 的阅读方向设置为 LTR
        if hasattr(self.book, 'direction'):
            self.book.direction = 'ltr'
        elif hasattr(self.book, 'spine'):
            # ebooklib may handle it via metadata or custom property, let's set metadata too
            self.book.set_direction('ltr')

        # 2. 管道处理 (Pipeline Processing)
        for item in self.book.get_items():
            item_type = item.get_type()
            # 9=Document(HTML), 2=Style(CSS)
            if item_type == 9 or item_type == 2:
                content = item.get_content()
                for cleaner in self.cleaners:
                    content = cleaner.process(content, item_type)
                item.set_content(content)

        # 3. 重新打包 (Repackage)
        packager = EpubPackager(self.book, self.output_path)
        success = packager.save()
        
        if success:
            print(f"✅ [Success] Output saved to: {self.output_path}")
        else:
            print("❌ [Error] Failed to save EPUB.")

        # 4. 输出翻译统计（如果启用了翻译）
        if self.enable_translation:
            for cleaner in self.cleaners:
                if hasattr(cleaner, 'stats'):
                    print(cleaner.stats.summary(cleaner.model))
            
        return success