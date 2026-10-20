"""Plain text stays text: strict Unicode decoding, escaped markup and bounded TOC."""
import html
import re
from pathlib import Path

_CHAPTER = re.compile(r'^(?:(?:chapter|part|book)\s+(?:\d+|[ivxlcdm]+)\b|第\s*[0-9０-９一二三四五六七八九十百千万零〇兩两]+\s*[章節节部卷話话回]|제?\s*[0-9０-９]+\s*[장화부](?:\s|[.:：]|$))', re.I)


def decode_txt(data: bytes) -> str:
    if len(data) > 60 * 1024 * 1024:
        raise ValueError('TXT 超过 60 MB 限制')
    try:
        encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
        text = data.decode(encoding, errors='strict')
    except UnicodeError as exc:
        raise ValueError('TXT 编码无法识别，请另存为 UTF-8 或带 BOM 的 UTF-16 后上传') from exc
    if not text.strip():
        raise ValueError('TXT 没有正文，未创建付费订单')
    if any(ord(c) < 32 and c not in '\t\n\r' or ord(c) in (0xfffe, 0xffff) for c in text):
        raise ValueError('TXT 含有不支持的控制字符，请导出为纯文本')
    return text.replace('\r\n', '\n').replace('\r', '\n')


def txt_to_html(path: Path):
    text = decode_txt(Path(path).read_bytes())
    language = 'en'
    if re.search(r'[\uac00-\ud7af]', text):
        language = 'ko'
    elif re.search(r'[\u3040-\u30ff]', text):
        language = 'ja'
    elif re.search(r'[\u3400-\u9fff]', text):
        from opencc import OpenCC
        language = 'zh-TW' if OpenCC('t2s').convert(text) != text else 'zh-CN'
    blocks, toc, paragraph = [], [], []
    def flush():
        if paragraph:
            blocks.append('<p>' + '<br/>'.join(html.escape(line) for line in paragraph) + '</p>')
            paragraph.clear()
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            flush()
        elif len(stripped) <= 120 and _CHAPTER.match(stripped) and len(toc) < 2000:
            flush()
            identity = 'txt-chapter-' + str(len(toc) + 1)
            toc.append({'id': identity, 'title': stripped})
            blocks.append(f'<h1 id="{identity}">{html.escape(stripped)}</h1>')
        else:
            paragraph.append(line)
    flush()
    return '\n'.join(blocks), {'title': Path(path).stem, 'language': language, 'toc': toc}
