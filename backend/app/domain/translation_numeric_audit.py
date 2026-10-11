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
    r"\s*(?P<suffix>%|(?:percent|per\s+cent)\b(?!\s+points?\b)|美元|英镑|欧元|人民币|日元|dollars|bucks)?", re.I)


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


# Explicit dates carry a unit. Never equate a decade with its first year or
# infer an omitted century from the current date, the book or nearby prose.
_CN_NUM = r'(?:\d{1,4}|[零〇一二三四五六七八九十百千]{1,8})'
_ERA = r'(?:B\.?\s*C\.?(?:\s*E\.?)?|C\.?\s*E\.?|A\.?\s*D\.?)'
_EN_START = rf'(?<![\w.+−‐‑-])(?:(?P<era_prefix>{_ERA})(?![A-Za-z])\s+)?'
_EN_JOIN = r'(?:\s+|\s*[-‐‑]\s*)'
_EN_PART = rf'(?:(?P<part>early|mid|late){_EN_JOIN}(?:in\s+the\s+)?)?'
_EN_ERA = rf'(?:\s+(?P<era>{_ERA})(?![A-Za-z]))?'
_CN_START = r'(?<![\d零〇一二三四五六七八九十百千])(?P<era>公元前|公元)?'
_CN_PART = r'(?P<part>初期|中期|末期|早期|晚期|初|中|末)?'
_TEMPORAL_PATTERNS = [
    ('cn_century_decade', re.compile(rf'{_CN_START}(?P<c>{_CN_NUM})\s*世纪\s*(?P<n>{_CN_NUM})\s*年代{_CN_PART}')),
    ('decade', re.compile(rf'{_EN_START}{_EN_PART}(?P<n>[1-9]\d{{1,2}}0)[\u2019\x27]?s\b{_EN_ERA}', re.I)),
    ('cn_decade', re.compile(rf'{_CN_START}(?P<n>{_CN_NUM})\s*年代{_CN_PART}')),
    ('century', re.compile(rf'{_EN_START}{_EN_PART}(?P<n>\d{{1,3}})(?P<ordinal>st|nd|rd|th){_EN_JOIN}century\b{_EN_ERA}', re.I)),
    ('cn_century', re.compile(rf'{_CN_START}(?P<n>{_CN_NUM})\s*世纪{_CN_PART}')),
]
_PARTS = {'early': 'early', 'mid': 'mid', 'late': 'late',
          '初期': 'early', '早期': 'early', '初': 'early',
          '中期': 'mid', '中': 'mid', '末期': 'late', '晚期': 'late', '末': 'late'}


def _temporal_facts(text):
    facts = []
    for form, pattern in _TEMPORAL_PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < f.end and match.end() > f.start for f in facts):
                continue
            raw_number = match['n']
            value = int(raw_number) if raw_number.isdecimal() else int(_chinese_number(raw_number))
            if form == 'century':
                ordinal = 'th' if 10 <= value % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(value % 10, 'th')
                if match['ordinal'].lower() != ordinal:
                    continue
            unit = 'century' if form in {'century', 'cn_century'} else 'decade'
            if form == 'cn_century_decade':
                raw_century = match['c']
                century = int(raw_century) if raw_century.isdecimal() else int(_chinese_number(raw_century))
                if not 1 <= century <= 100 or not 0 <= value <= 90 or value % 10:
                    continue
                value += (century - 1) * 100
            if (unit == 'decade' and (not 100 <= value <= 9990 or value % 10)
                    or unit == 'century' and not 1 <= value <= 100):
                continue
            eras = [e for e in (match.groupdict().get('era_prefix'), match['era']) if e]
            normalized_eras = {'bce' if e == '公元前' or re.sub(r'[^A-Z]', '', e.upper()) in {'BC', 'BCE'} else 'ce' for e in eras}
            era = 'conflicting-era' if len(normalized_eras) > 1 else next(iter(normalized_eras), 'ce')
            part = _PARTS.get((match['part'] or '').lower(), 'whole')
            # Do not reduce a compound qualifier to its last/first endpoint.
            # Until range semantics are modeled, keep its original spelling as
            # an unresolved unit rather than accepting a simpler translation.
            before = re.search(r'\b(?:early|mid|late)\s+(?:to|and|or)\s+(?:the\s+)?$', text[:match.start()], re.I)
            after = re.match(r'(?:至|到|及|和|或|[-–—])(?:初期|中期|末期|早期|晚期|初|中|末)', text[match.end():])
            if before or after:
                part = f'unresolved:{before.group().lower() if before else ""}:{part}:{after.group() if after else ""}'
            facts.append(NumericFact(Decimal(value), f'{unit}:{era}:{part}', match.group(), match.start(), match.end(), 1))
    return facts


def numeric_facts(text):
    text = unicodedata.normalize("NFKC", text or "")
    temporal = _temporal_facts(text)
    facts = []
    for m in _TOKEN.finditer(text):
        if any(m.start() < f.end and m.end() > f.start for f in temporal):
            continue
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
        suffix = re.sub(r'\s+', ' ', (m['suffix'] or '').lower())
        kind = 'percent' if m['percent'] or suffix in {'%', 'percent', 'per cent'} else _CURRENCIES.get((m['currency'] or m['suffix'] or m['scale'] or '').lower(), 'number')
        facts.append(NumericFact(value, kind, m.group().strip(), m.start(), m.end(), scale))
    facts = sorted(facts + temporal, key=lambda f: f.start)
    # Only explicit numeric ranges share a trailing magnitude/currency.
    for left, right in zip(facts, facts[1:]):
        if left.kind.startswith(('decade:', 'century:')) or right.kind.startswith(('decade:', 'century:')):
            continue
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
