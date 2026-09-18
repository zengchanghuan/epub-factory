"""Letter eligibility is script-aware, not restricted to ASCII English."""
import unicodedata


def is_han(ch: str) -> bool:
    n = ord(ch)
    return (0x3400 <= n <= 0x9fff or 0xf900 <= n <= 0xfaff
            or 0x20000 <= n <= 0x323af)


def has_foreign_letters(text: str, target_lang: str = 'zh-CN') -> bool:
    target = (target_lang or '').lower()
    for ch in text:
        if not unicodedata.category(ch).startswith('L'):
            continue
        if target.startswith('zh') and is_han(ch):
            continue
        if target.startswith('ja') and (is_han(ch) or 0x3040 <= ord(ch) <= 0x30ff):
            continue
        if target.startswith('ko') and (0xac00 <= ord(ch) <= 0xd7af or 0x1100 <= ord(ch) <= 0x11ff):
            continue
        return True
    return False
