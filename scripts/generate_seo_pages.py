import os
import re

def generate_seo_pages():
    frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'frontend')
    index_path = os.path.join(frontend_dir, 'index.html')
    
    with open(index_path, 'r', encoding='utf-8') as f:
        base_html = f.read()

    pages = [
        {
            "filename": "epub-translator.html",
            "title": "Best AI EPUB Translator – 免费沉浸式 AI 电子书翻译工具",
            "description": "专业的 AI EPUB 翻译器，支持中英双语对照、沉浸式译文、完整保留电子书格式、目录结构及插图。免费在线使用。",
            "keywords": "EPUB翻译, AI翻译电子书, epub translator, 双语对照电子书, 英文书翻译中文, 沉浸式阅读",
            "h1": "AI 电子书翻译工具 (EPUB/PDF)",
            "subtitle": "利用先进的大语言模型，一键将英文原著翻译为优雅的中文。完美保留插图、目录与排版格式，支持中英双语对照，给您带来最佳阅读体验。",
            "canonical": "https://fixepub.com/epub-translator.html",
            "og_title": "Best AI EPUB Translator – 免费沉浸式 AI 电子书翻译工具",
            "og_desc": "专业的 AI EPUB 翻译器，支持双语对照，完美保留格式与目录，让英文原著阅读不再有门槛。"
        },
        {
            "filename": "vertical-to-horizontal.html",
            "title": "EPUB 竖排转横排在线工具 – 一键完美修复排版",
            "description": "免费的 EPUB 竖排转横排转换器。一键将日文轻小说、台湾繁体书籍的竖排格式转化为符合手机/Kindle阅读习惯的横排格式，完美修复排版问题。",
            "keywords": "EPUB竖排转横排, 竖排转横排, 电子书横排, epub排版修复, epubhv, 轻小说竖排转横排",
            "h1": "EPUB 竖排转横排转换器",
            "subtitle": "一键将原版轻小说或台版繁体书的竖版排版，转换为符合现代阅读习惯的横向排版。全面修复溢出、断层等样式问题，专为 Kindle 与 Apple Books 优化。",
            "canonical": "https://fixepub.com/vertical-to-horizontal.html",
            "og_title": "EPUB 竖排转横排在线工具 – 一键完美修复排版",
            "og_desc": "免费在线 EPUB 竖排转横排工具。彻底解决轻小说、台版书籍在手机和 Kindle 上的阅读障碍。"
        },
        {
            "filename": "traditional-to-simplified.html",
            "title": "EPUB 繁简转换在线工具 – 完美保留电子书格式",
            "description": "免费专业的 EPUB 繁简转换工具。支持繁体转简体、简体转繁体。内置 OpenCC 引擎，完美处理台湾、香港词汇差异，100%保留原书排版与目录结构。",
            "keywords": "EPUB繁简转换, 繁体转简体, EPUB简转繁, 电子书繁简转换, OpenCC, 台湾繁体",
            "h1": "EPUB 繁简转换工具",
            "subtitle": "基于强劲的转换引擎，不仅仅是文字替换，更能智能处理两岸三地用词差异，全面优化 CSS 代码并保持原始目录与版式完整。",
            "canonical": "https://fixepub.com/traditional-to-simplified.html",
            "og_title": "EPUB 繁简转换在线工具 – 完美保留电子书格式",
            "og_desc": "支持精准的 EPUB 繁简互转，智能处理地域词汇，保留 100% 的排版与目录结构，免费使用。"
        }
    ]

    for p in pages:
        html = base_html
        
        # Replace title
        html = re.sub(r'<title>.*?</title>', f'<title>{p["title"]}</title>', html, flags=re.IGNORECASE)
        # Replace description
        html = re.sub(r'<meta name="description" content=".*?"\s*/>', f'<meta name="description" content="{p["description"]}" />', html)
        # Replace keywords
        html = re.sub(r'<meta name="keywords" content=".*?"\s*/>', f'<meta name="keywords" content="{p["keywords"]}" />', html)
        # Replace canonical
        html = re.sub(r'<link rel="canonical" href=".*?"\s*/>', f'<link rel="canonical" href="{p["canonical"]}" />', html)
        
        # Replace OG tags
        html = re.sub(r'<meta property="og:title" content=".*?"\s*/>', f'<meta property="og:title" content="{p["og_title"]}" />', html)
        html = re.sub(r'<meta property="og:description" content=".*?"\s*/>', f'<meta property="og:description" content="{p["og_desc"]}" />', html)
        html = re.sub(r'<meta property="og:url" content=".*?"\s*/>', f'<meta property="og:url" content="{p["canonical"]}" />', html)
        
        # Replace Twitter tags
        html = re.sub(r'<meta name="twitter:title" content=".*?"\s*/>', f'<meta name="twitter:title" content="{p["og_title"]}" />', html)
        html = re.sub(r'<meta name="twitter:description" content=".*?"\s*/>', f'<meta name="twitter:description" content="{p["og_desc"]}" />', html)
        
        # Replace H1
        html = re.sub(r'<h1>.*?</h1>', f'<h1>{p["h1"]}</h1>', html, count=1)
        # Replace Subtitle (the <p> right after h1)
        html = re.sub(r'<h1>.*?</h1>\s*<p>.*?</p>', f'<h1>{p["h1"]}</h1>\n      <p>{p["subtitle"]}</p>', html, flags=re.DOTALL, count=1)
        
        # Replace PayPal client ID with placeholder so it can be updated easily later
        html = re.sub(r'<script src="https://www\.paypal\.com/sdk/js\?client-id=[^&]+&currency=USD"></script>', '<script src="https://www.paypal.com/sdk/js?client-id=ATzusU6RWdPLzCg2JzeRuNMuXfrF0yDLNpCQe-aQbuP5yTtfEAEhlkkB1gcmSJM1ey5GVg_ngtIyYdTz&currency=USD"></script>', html)
        
        # Write to file
        out_path = os.path.join(frontend_dir, p["filename"])
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"Generated {p['filename']}")

if __name__ == '__main__':
    generate_seo_pages()
