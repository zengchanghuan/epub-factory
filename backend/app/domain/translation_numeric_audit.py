"""Conservative numeric equivalence. Ambiguous idioms remain review signals."""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import re
import unicodedata

_SCALES = {"thousand": 1000, "million": 10**6, "billion": 10**9,
           "trillion": 10**12, "k": 1000, "grand": 1000, "百": 100, "千": 1000, "万": 10**4, "亿": 10**8, "万亿": 10**12}
_CURRENCIES = {"$": "usd", "美元": "usd", "£": "gbp", "英镑": "gbp",
               "€": "eur", "欧元": "eur", "人民币": "cny", "日元": "jpy",
               "dollars": "usd", "bucks": "usd", "grand": "usd"}
_DIGITS = dict(zip("零〇一二三四五六七八九两", [0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))
_TOKEN = re.compile(
    r"(?P<percent>百分之)?(?P<leading_sign>negative\s+|[+−负-])?(?P<currency>[$£€])?\s*(?P<sign>[+−负-])?"
    r"(?P<number>\d+(?:,\d{3})*(?:\.\d+)?|\.\d+|[零〇一二三四五六七八九两十百千万亿]+)"
    r"(?P<ordinal>st|nd|rd|th)?\s*(?P<scale>thousand|million|billion|trillion|grand|k(?![a-z])|万亿|万|亿|千|百)?"
    r"\s*(?P<suffix>%|美元|英镑|欧元|人民币|日元|dollars|bucks)?", re.I)


def _chinese_number(text):
    if all(ch in _DIGITS for ch in text):
        return Decimal(''.join(str(_DIGITS[ch]) for ch in text))
    total = section = digit = 0
    for ch in text:
        if ch in _DIGITS:
            digit = _DIGITS[ch]
        elif ch in "十百千":
            section += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[ch]
            digit = 0
        elif ch == "万":
            section = (section + digit) * 10000
            digit = 0
        elif ch == "亿":
            total += (section + digit) * 10**8
            section = digit = 0
    return Decimal(total + section + digit)


@dataclass
class NumericFact:
    value: Decimal
    kind: str
    raw: str
    start: int
    end: int
    scale: int


def numeric_facts(text):
    text = unicodedata.normalize("NFKC", text or "")
    facts = []
    for m in _TOKEN.finditer(text):
        number = m['number']
        if (len(number) == 1 and number in _DIGITS and not m['percent']
                and not re.match(r'(?:年|月|日|岁|个|次|局|点|位|章|节|美元|英镑|欧元|%)', text[m.end('number'):])):
            continue  # “一直/一旦/二次发行” is not reliable evidence of a quantity.
        scale = _SCALES.get((m['scale'] or '').lower(), 1)
        value = (Decimal(number.replace(',', '')) if number[0].isdigit() or number.startswith('.') else _chinese_number(number)) * scale
        # A hyphen after a numeral is a range separator, not a minus sign.
        sign = m['leading_sign'] or m['sign']
        sign_pos = m.start('leading_sign') if m['leading_sign'] else m.start('sign') if m['sign'] else -1
        range_minus = (sign == '-' and ((facts and not text[facts[-1].end:sign_pos].strip())
                                       or (sign_pos > 0 and text[sign_pos-1].isascii() and text[sign_pos-1].isalpha())))
        if (sign in {'-', '−', '负'} or str(sign).lower().startswith('negative')) and not range_minus:
            value = -value
        kind = 'percent' if m['percent'] or m['suffix'] == '%' else _CURRENCIES.get((m['currency'] or m['suffix'] or m['scale'] or '').lower(), 'number')
        facts.append(NumericFact(value, kind, m.group().strip(), m.start(), m.end(), scale))
    # Only explicit numeric ranges share a trailing magnitude/currency.
    for left, right in zip(facts, facts[1:]):
        gap = text[left.end:right.start].strip()
        # A consumed range hyphen belongs to right.raw, not gap.
        is_range = bool(re.fullmatch(r'(?:to|or|至|到|或|[-–—])', gap, re.I)) or (not gap and right.raw.startswith('-'))
        if is_range:
            if left.scale == 1 and right.scale > 1 and 0 <= left.value <= right.value / right.scale:
                left.value *= right.scale
            if left.kind == 'number' and right.kind != 'number':
                left.kind = right.kind
            elif right.kind == 'number' and left.kind != 'number':
                right.kind = left.kind
    return facts


def missing_numeric_facts(source, target):
    available = Counter((f.value, f.kind) for f in numeric_facts(target))
    missing = []
    for fact in numeric_facts(source):
        key = (fact.value, fact.kind)
        if available[key]:
            available[key] -= 1
        else:
            missing.append(fact.raw)
    return missing
