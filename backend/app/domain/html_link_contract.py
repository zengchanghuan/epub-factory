"""Source-owned link/anchor attributes at model, cache and reduce boundaries.

Never infer targets from translated labels or fetch URLs. Attribute restoration
is allowed only when the entire element topology still matches the source.
"""
from copy import deepcopy
from bs4 import BeautifulSoup, NavigableString, Tag

_OUTER = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
_ATTRS = {"id", "xml:id", "href", "xlink:href", "src", "srcset", "poster", "data",
          "epub:type", "epub-type", "role", "class"}


def _fragment(raw):
    soup = BeautifulSoup(raw or "", "html.parser")
    nodes = [n for n in soup.contents if not isinstance(n, NavigableString) or str(n).strip()]
    if len(nodes) == 1 and isinstance(nodes[0], Tag) and nodes[0].name in _OUTER:
        # The reducer retains the source outer block and only writes its inner
        # HTML; legacy caches may return either the block or its contents.
        nodes[0].unwrap()
    return soup


def _elements(soup):
    result = []
    stack = [(soup, ())]
    while stack:
        parent, path = stack.pop()
        children = [n for n in parent.children if isinstance(n, Tag)]
        for index, node in reversed(list(enumerate(children))):
            node_path = (*path, index)
            result.append((node_path, node))
            stack.append((node, node_path))
    return sorted(result, key=lambda entry: entry[0])


def _attributes(node):
    keys = _ATTRS | ({"name", "rel", "target", "download"} if node.name == "a" else set())
    return {key: node.attrs[key] for key in keys if key in node.attrs}


def preserves_link_contract(source, translated):
    def signature(raw):
        return [(path, node.name, _attributes(node)) for path, node in _elements(_fragment(raw))
                if _attributes(node) or node.name == "a"]
    return signature(source) == signature(translated)


def restore_link_attributes(source, translated):
    """Keep translated text; restore only source-owned structural attributes."""
    source_soup, translated_soup = _fragment(source), _fragment(translated)
    left, right = _elements(source_soup), _elements(translated_soup)
    if [(p, n.name) for p, n in left] != [(p, n.name) for p, n in right]:
        return translated, False
    changed = False
    for (_, original), (_, target) in zip(left, right):
        expected = _attributes(original)
        if expected == _attributes(target):
            continue
        changed = True
        for key in _attributes(target):
            del target.attrs[key]
        target.attrs.update(deepcopy(expected))
    return (str(translated_soup), True) if changed else (translated, False)
