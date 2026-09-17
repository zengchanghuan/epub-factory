"""Deterministically annotate confirmed glossary translations in XHTML output."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString


_SKIP_PARENTS = {"script", "style", "title", "head", "code", "pre"}


def highlight_confirmed_terms(
    content: bytes,
    glossary: dict[str, str],
) -> tuple[bytes, int]:
    target_to_source: dict[str, str] = {}
    for source, target in glossary.items():
        source_text = str(source or "").strip()
        target_text = str(target or "").strip()
        if source_text and target_text and len(target_text) >= 2:
            target_to_source.setdefault(target_text, source_text)
    if not target_to_source:
        return content, 0

    soup = BeautifulSoup(content, "html.parser")
    targets = sorted(target_to_source, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(term) for term in targets))
    count = 0
    for node in list(soup.find_all(string=True)):
        parent = node.parent
        if (
            not isinstance(node, NavigableString)
            or not parent
            or parent.name in _SKIP_PARENTS
            or parent.find_parent(class_="epub-term")
            or "epub-term" in (parent.get("class") or [])
        ):
            continue
        text = str(node)
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        replacements = []
        cursor = 0
        for match in matches:
            if match.start() > cursor:
                replacements.append(NavigableString(text[cursor:match.start()]))
            target = match.group(0)
            span = soup.new_tag("span")
            span["class"] = ["epub-term"]
            span["data-original"] = target_to_source[target]
            span["title"] = f"{target_to_source[target]} → {target}"
            span.string = target
            replacements.append(span)
            cursor = match.end()
            count += 1
        if cursor < len(text):
            replacements.append(NavigableString(text[cursor:]))
        for replacement in reversed(replacements):
            node.insert_after(replacement)
        node.extract()

    if count and soup.head and not soup.head.find("style", attrs={"data-epub-term-style": "1"}):
        style = soup.new_tag("style")
        style["data-epub-term-style"] = "1"
        style.string = (
            ".epub-term{border-bottom:1px dotted currentColor;"
            "text-decoration:none}.epub-term[data-original]{cursor:help}"
        )
        soup.head.append(style)
    return soup.encode("utf-8"), count
