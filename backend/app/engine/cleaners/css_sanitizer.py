from bs4 import BeautifulSoup
import re

class CssSanitizer:
    def process(self, content: bytes, item_type: int) -> bytes:
        # 如果是 HTML 文件 (ebooklib item_type == 9)
        if item_type == 9:
            # BeautifulSoup expects bytes or string
            soup = BeautifulSoup(content, 'xml')
            
            # 清理所有标签内的 style 属性中的绝对单位
            for tag in soup.find_all(style=True):
                style_str = tag['style']
                
                # 正则暴力替换：删除 font-family
                style_str = re.sub(r'font-family\s*:[^;]+;?', '', style_str)
                # 删除绝对行高 line-height: 20px
                style_str = re.sub(r'line-height\s*:\s*\d+px;?', '', style_str)
                # 将背景色静默
                style_str = re.sub(r'background-color\s*:[^;]+;?', '', style_str)
                
                tag['style'] = style_str.strip()
                
                # 如果 style 被洗空了，直接删除 style 属性
                if not tag['style']:
                    del tag['style']
                    
            # 解决深层嵌套（DOM 瘦身示例：剥离无用的 span）
            # 注意：这可能会破坏原本为了应用样式的 span，作为例子先保留，如果是P0清理
            for span in soup.find_all('span', class_=False, style=False):
                span.unwrap()

            # encode back to bytes
            return str(soup).encode('utf-8')
            
        # 如果是独立的 CSS 文件 (ebooklib item_type == 2)，暂时原样返回
        return content