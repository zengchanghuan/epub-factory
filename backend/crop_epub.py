import os
import sys
from pathlib import Path

# Add backend directory to sys.path to allow imports
sys.path.append(str(Path(__file__).parent))

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

def crop_epub(input_path: str, output_path: str, max_text_chapters: int = 3):
    print(f"📦 Loading EPUB: {input_path}")
    book = epub.read_epub(input_path)
    
    text_chapters_kept = 0
    
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            content = item.get_content()
            soup = BeautifulSoup(content, 'xml')
            text_content = soup.get_text(strip=True)
            
            # 只有当该页面有实质性内容（>500字符），才算作一个“章节”
            if len(text_content) > 500:
                if text_chapters_kept >= max_text_chapters:
                    # 超过配额的章节，直接将其内容清空，这样翻译器就不会处理它了，从而省 token
                    # 保持基本的 HTML 结构以防 EPUB 报错
                    empty_content = b'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cropped</title></head><body><p>This chapter was cropped to save tokens.</p></body></html>'
                    item.set_content(empty_content)
                else:
                    text_chapters_kept += 1
                    print(f"✅ Kept chapter: {item.get_name()} (Length: {len(text_content)} chars)")
            else:
                # 内容太少（如版权页、封面、只有标题的页面），保留，反正不耗多少 token
                pass

    epub.write_epub(output_path, book)
    print(f"🎉 Cropped EPUB saved to: {output_path}")

if __name__ == "__main__":
    workspace_dir = Path("/Users/zengchanghuan/Desktop/workspace/epub-factory")
    input_file = workspace_dir / "How the World Really Works The Science Behind How We Got Here and Where Were Going (Vaclav Smil) (z-library.sk, 1lib.sk, z-lib.sk).epub"
    output_file = workspace_dir / "backend" / "test_en.epub"
    
    if not input_file.exists():
        print(f"❌ Input file not found: {input_file}")
        sys.exit(1)
        
    crop_epub(str(input_file), str(output_file), max_text_chapters=3)