"""Letter eligibility is script-aware, not restricted to ASCII English."""
import unicodedata
from functools import lru_cache

LANGUAGES = {"zh-CN": "简体中文", "zh-TW": "繁體中文", "en": "English", "ja": "日本語", "ko": "한국어"}

@lru_cache(maxsize=2)
def _converter(target):
    from opencc import OpenCC
    return OpenCC("s2t" if target == "zh-tw" else "t2s")



def is_han(ch: str) -> bool:
    n = ord(ch)
    return (0x3400 <= n <= 0x9fff or 0xf900 <= n <= 0xfaff
            or 0x20000 <= n <= 0x323af)


def has_foreign_letters(text: str, target_lang: str = 'zh-CN', source_lang: str = 'auto') -> bool:
    target = (target_lang or '').lower()
    if source_lang != 'auto' and source_lang.lower() != target:
        return any(unicodedata.category(ch).startswith('L') for ch in text)
    if target.startswith('zh') and _converter(target).convert(text) != text:
        return True
    for ch in text:
        if not unicodedata.category(ch).startswith('L'):
            continue
        if target.startswith('zh') and is_han(ch):
            continue
        if target.startswith('ja') and 0x3040 <= ord(ch) <= 0x30ff:
            continue
        if target.startswith('ko') and (0xac00 <= ord(ch) <= 0xd7af or 0x1100 <= ord(ch) <= 0x11ff):
            continue
        return True
    return False
