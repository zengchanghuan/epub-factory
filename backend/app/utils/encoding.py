"""
解码层：编码探测 + 回退。

EPUB 规范为 UTF-8；遇历史文件或错误声明时，先尝试 UTF-8，失败则按常见中文编码回退。
可选依赖 chardet 用于探测，未安装时仅使用固定回退列表。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("epub_factory.encoding")

# 常见中文/东亚编码，按优先级
_FALLBACK_ENCODINGS = ("utf-8", "big5", "gbk", "gb18030", "cp950", "utf-8-sig")


def decode_with_fallback(content: bytes) -> str:
    """
    将字节解码为 str：优先 UTF-8，失败时尝试 chardet 探测或固定回退列表。
    始终返回字符串，不抛异常；无法解码时用 replace 保证可读。
    """
    if not content:
        return ""

    # 1) 严格 UTF-8
    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass

    # 2) 可选 chardet 探测
    enc = _detect_encoding(content)
    if enc:
        try:
            return content.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass

    # 3) 固定回退列表（跳过已尝试的 utf-8）
    for enc in _FALLBACK_ENCODINGS:
        if enc == "utf-8":
            continue
        try:
            return content.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue

    # 4) 最后保底
    return content.decode("utf-8", errors="replace")


def _detect_encoding(content: bytes) -> Optional[str]:
    """若已安装 chardet，返回探测到的编码名；否则返回 None。"""
    try:
        import chardet
        result = chardet.detect(content)
        enc = (result or {}).get("encoding")
        if enc:
            # 归一化常见别名
            enc_lower = enc.lower().replace("-", "_")
            if enc_lower in ("utf_8", "utf8"):
                return "utf-8"
            if enc_lower in ("big5", "big5_tw"):
                return "big5"
            if enc_lower in ("gbk", "gb2312", "gb18030"):
                return "gbk"
            return enc
    except ImportError:
        pass
    except Exception as e:
        logger.debug("chardet detect failed: %s", e)
    return None
