"""Remove only absent optional font sources, preserving readable fallbacks."""
import re
from urllib.parse import unquote, urlsplit

FONT_FACE = re.compile(r'@font-face\s*\{(?:[^{}"\x27]|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27)*\}', re.I)


def repair_font_sources(text, stylesheet, root):
    if '@font-face' not in text.lower() or 'url(' not in text.lower(): return text, 0
    import cssutils
    parser = cssutils.CSSParser(validate=False, fetcher=lambda _url: (None, None))
    removed = 0

    def repair(match):
        nonlocal removed
        sheet = parser.parseString(match.group(0))
        if len(sheet.cssRules) != 1: return match.group(0)
        rule = sheet.cssRules[0]
        if rule.type != rule.FONT_FACE_RULE: return match.group(0)
        prop = rule.style.getProperty('src')
        if prop is None: return match.group(0)
        groups = [[]]
        for part in prop.propertyValue.seq:
            if part.type == 'operator' and part.value == ',': groups.append([])
            else: groups[-1].append(part)
        kept, changed = [], False
        for group in groups:
            absent = False
            for part in group:
                if part.type != 'URIValue': continue
                link = urlsplit(part.value.uri)
                if link.scheme or link.netloc: continue
                target = (stylesheet.parent / unquote(link.path)).resolve()
                if not target.is_relative_to(root.resolve()) or not target.is_file(): absent = True
            if absent: removed += 1; changed = True
            else: kept.append(' '.join(part.value if isinstance(part.value, str) else part.value.cssText for part in group))
        if not changed: return match.group(0)
        if not kept: return ''
        rule.style.setProperty('src', ', '.join(kept))
        return rule.cssText.decode('utf-8') if isinstance(rule.cssText, bytes) else rule.cssText

    return FONT_FACE.sub(repair, text), removed
