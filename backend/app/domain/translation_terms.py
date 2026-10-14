"""Shared, deterministic terminology matching; never infer a new translation."""

import re
import unicodedata
from html import escape, unescape
from html.parser import HTMLParser

from bs4 import BeautifulSoup

_BLOCK_TAGS = {"p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "header", "footer", "table", "tr", "td", "th", "dt", "dd"}


def visible_term_text(html: str) -> str:
    from app.engine.chunk_extractor import NON_TEXT_TAGS
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup.find_all(lambda tag: tag.name.split(":")[-1] in NON_TEXT_TAGS | {"code", "pre"}
                              or "epub-original" in (tag.get("class") or [])):
        if node.parent is not None:
            node.insert_before(" ")
            node.decompose()
    for br in soup.find_all("br"):
        br.replace_with(" ")
    # Inline small caps must join, but separate paragraphs must not join.
    for block in soup.find_all(list(_BLOCK_TAGS)):
        block.insert_before(" ")
        block.insert_after(" ")
    return re.sub(r"\s+", " ", soup.get_text("", strip=False)).strip()


def _latin_word_char(char: str) -> bool:
    return (char == "_" or char.isdigit() or "LATIN" in unicodedata.name(char, "")
            or unicodedata.category(char).startswith("M"))


def term_spans(term: str, text: str) -> list[tuple[int, int]]:
    if not term or not term.strip() or not text:
        return []
    latin = any("LATIN" in unicodedata.name(c, "") for c in term) and all(
        _latin_word_char(c) or c.isspace() or c in "'’.-" for c in term
    )
    pattern = r"\s+".join(re.escape(part) for part in term.split())
    spans = []
    for match in re.finditer(pattern, text, re.I if latin else 0):
        start, end = match.span()
        # Python's \b treats adjacent Chinese as part of the English word.
        # Accept 中文Aster中文, but never match Aster inside Asterisk/ÉAster.
        if latin and ((start and _latin_word_char(text[start - 1]))
                      or (end < len(text) and _latin_word_char(text[end]))):
            continue
        spans.append((start, end))
    return spans


def ambiguous_short_name(source: str, target: str) -> bool:
    """A capitalized Latin name mapped to one CJK character needs context."""
    return (len(target.strip()) == 1 and "CJK" in unicodedata.name(target.strip(), "")
            and len(source.strip()) > 1 and source.strip()[0].isupper()
            and "LATIN" in unicodedata.name(source.strip()[0], ""))


def target_term_spans(term: str, text: str, *, source_term: str = "",
                      require_isolated_short: bool = False) -> list[tuple[int, int]]:
    """A one-character CJK translation needs isolated evidence, not 文 in 文献.

    This is deliberately conservative: an embedded single-character surname
    needs a full-name mapping to be verified. Source matching stays unchanged.
    """
    spans = term_spans(term, text)
    if ambiguous_short_name(source_term, term) or (require_isolated_short
            and len(term.strip()) == 1 and "CJK" in unicodedata.name(term.strip(), "")):
        return [(a, b) for a, b in spans if (a == 0 or not text[a - 1].isalnum())
                and (b == len(text) or not text[b].isalnum())]
    return spans


def target_term_present(term: str, text: str, *, source_term: str = "",
                        require_isolated_short: bool = False) -> bool:
    return bool(target_term_spans(term, text, source_term=source_term,
                                 require_isolated_short=require_isolated_short))


def parenthetical_spans(text: str) -> list[tuple[int, int]]:
    """Balanced ASCII/CJK parentheses, including nested annotations."""
    stack, spans = [], []
    for index, char in enumerate(text):
        if char in "(（":
            stack.append((char, index))
        elif char in ")）" and stack and stack[-1][0] == {")": "(", "）": "（"}[char]:
            _, start = stack.pop()
            spans.append((start, index + 1))
    return sorted(spans)


def relevant_term_occurrences(glossary: dict[str, str], text: str, *, limit: int | None = None) -> list[tuple[int, int, str, str]]:
    """Longest non-overlapping source phrases; limits count distinct terms."""
    if limit is not None and limit <= 0:
        return []
    matches = [(a, b, src, dst) for src, dst in (glossary or {}).items()
               if isinstance(src, str) and isinstance(dst, str) and src.strip() and dst.strip()
               for a, b in term_spans(src, text)]
    matches.sort(key=lambda m: (-(m[1] - m[0]), m[0], m[2]))
    selected = []
    names = set()
    for a, b, src, dst in matches:
        if limit is not None and src not in names and len(names) >= limit:
            continue
        if any(a < end and b > start for start, end, *_ in selected):
            continue
        selected.append((a, b, src, dst))
        names.add(src)
    selected.sort(key=lambda m: m[0])
    return selected


def relevant_terms(glossary: dict[str, str], text: str, *, limit: int | None = None) -> dict[str, str]:
    return {src: dst for _, _, src, dst in relevant_term_occurrences(glossary, text, limit=limit)}


def replace_term_text(html: str, edits_for_text) -> str:
    """Apply text edits at original offsets without reserializing any markup.

    Match against decoded, joined text so entities and inline markup cannot
    invent word/URL boundaries. The callback receives an is_editable predicate:
    only ranges wholly inside one unprotected data node can be rewritten.
    Entity/inline-split names are left for review; markup stays byte-for-byte.
    """
    from app.engine.chunk_extractor import NON_TEXT_TAGS
    # HTMLParser counts LF only, not bare CR or Unicode paragraph separators.
    offsets = [0] + [match.end() for match in re.finditer("\n", html)]
    parts = []
    editable = []
    text_length = 0

    class TextEdits(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.stack = []

        def append_text(self, data, *, raw_offset=None):
            nonlocal text_length
            parts.append(data)
            if raw_offset is not None:
                editable.append((text_length, text_length + len(data), raw_offset))
            text_length += len(data)

        def handle_starttag(self, tag, attrs):
            local = tag.split(":")[-1]
            if local in _BLOCK_TAGS | {"br", "hr", "pre"}:
                self.append_text("\n")
            if local in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                return
            protected = (bool(self.stack and self.stack[-1][1])
                         or local in NON_TEXT_TAGS | {"code", "pre"}
                         or "epub-original" in (dict(attrs).get("class") or "").split())
            self.stack.append((tag, protected))

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

        def handle_endtag(self, tag):
            if tag.split(":")[-1] in _BLOCK_TAGS | {"pre"}:
                self.append_text("\n")
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break

        def handle_data(self, data):
            line, column = self.getpos()
            offset = offsets[line - 1] + column
            self.append_text(data, raw_offset=None if self.stack and self.stack[-1][1] else offset)

        def handle_entityref(self, name):
            self.append_text(unescape("&" + name + ";"))

        def handle_charref(self, name):
            self.append_text(unescape("&#" + name + ";"))

    parser = TextEdits()
    parser.feed(html)
    parser.close()
    def raw_span(start, end):
        for a, b, offset in editable:
            if a <= start < end <= b:
                return offset + start - a, offset + end - a
        return None

    edits = []
    for start, end, target in edits_for_text("".join(parts), lambda a, b: raw_span(a, b) is not None):
        span = raw_span(start, end)
        if span is not None:
            edits.append((*span, escape(target, quote=False)))
    for start, end, target in sorted(edits, reverse=True):
        html = html[:start] + target + html[end:]
    return html
