import re
import shutil
import tempfile
import zipfile
import posixpath
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag, urlparse, urlsplit, urlunsplit

import ebooklib
from ebooklib import epub
from ebooklib.utils import get_pages
from bs4 import BeautifulSoup, Comment
from lxml import etree
from .font_compat import repair_font_sources

# ebooklib 的已知 Bug：它的 XML 解析器会把所有属性名强制小写，
# 但 SVG 规范中的 preserveAspectRatio、viewBox 等属性是大小写敏感的。
# 小写后阅读器无法识别，导致图片缩放/显示异常。
SVG_CASE_FIXES = {
    'preserveaspectratio': 'preserveAspectRatio',
    'viewbox': 'viewBox',
    'basefrequency': 'baseFrequency',
    'calcmode': 'calcMode',
    'clippathunits': 'clipPathUnits',
    'contentscripttype': 'contentScriptType',
    'contentstyletype': 'contentStyleType',
    'diffuseconstant': 'diffuseConstant',
    'edgemode': 'edgeMode',
    'filterunits': 'filterUnits',
    'glyphref': 'glyphRef',
    'gradienttransform': 'gradientTransform',
    'gradientunits': 'gradientUnits',
    'kernelmatrix': 'kernelMatrix',
    'kernelunitlength': 'kernelUnitLength',
    'keypoints': 'keyPoints',
    'keysplines': 'keySplines',
    'keytimes': 'keyTimes',
    'lengthadjust': 'lengthAdjust',
    'limitingconeangle': 'limitingConeAngle',
    'markerheight': 'markerHeight',
    'markerunits': 'markerUnits',
    'markerwidth': 'markerWidth',
    'maskcontentunits': 'maskContentUnits',
    'maskunits': 'maskUnits',
    'numoctaves': 'numOctaves',
    'pathlength': 'pathLength',
    'patterncontentunits': 'patternContentUnits',
    'patterntransform': 'patternTransform',
    'patternunits': 'patternUnits',
    'pointsatx': 'pointsAtX',
    'pointsaty': 'pointsAtY',
    'pointsatz': 'pointsAtZ',
    'repeatcount': 'repeatCount',
    'repeatdur': 'repeatDur',
    'requiredextensions': 'requiredExtensions',
    'requiredfeatures': 'requiredFeatures',
    'specularconstant': 'specularConstant',
    'specularexponent': 'specularExponent',
    'spreadmethod': 'spreadMethod',
    'startoffset': 'startOffset',
    'stddeviation': 'stdDeviation',
    'stitchtiles': 'stitchTiles',
    'surfacescale': 'surfaceScale',
    'systemlanguage': 'systemLanguage',
    'tablevalues': 'tableValues',
    'targetx': 'targetX',
    'targety': 'targetY',
    'textlength': 'textLength',
    'xchannelselector': 'xChannelSelector',
    'ychannelselector': 'yChannelSelector',
    'zoomandpan': 'zoomAndPan',
}

XML_NAMESPACE_URIS = {
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
}


def _fix_svg_attributes(text: str) -> str:
    for wrong, correct in SVG_CASE_FIXES.items():
        text = re.sub(
            rf'\b{wrong}=',
            f'{correct}=',
            text
        )
    return text


def _fix_missing_xml_namespaces(text: str) -> str:
    """Restore namespace declarations dropped by ebooklib serialization."""
    if re.search(r"<svg(?=[\s>])", text, flags=re.IGNORECASE):
        text = re.sub(
            r"<svg(?![^>]*\bxmlns\s*=)(?=[\s>])",
            '<svg xmlns="http://www.w3.org/2000/svg"',
            text,
            flags=re.IGNORECASE,
        )

    for prefix, uri in XML_NAMESPACE_URIS.items():
        if not re.search(rf"(?:<|\s){re.escape(prefix)}:", text, flags=re.IGNORECASE):
            continue
        if re.search(rf"\bxmlns:{re.escape(prefix)}\s*=", text, flags=re.IGNORECASE):
            continue

        text, replacements = re.subn(
            r"<html(?=[\s>])",
            f'<html xmlns:{prefix}="{uri}"',
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if replacements == 0:
            text = re.sub(
                r"<svg:svg(?=[\s>])",
                f'<svg:svg xmlns:{prefix}="{uri}"',
                text,
                count=1,
                flags=re.IGNORECASE,
            )
    return text


def _fix_svg_path_serialization(text: str) -> str:
    """
    Restore empty SVG path elements that ebooklib's HTML parser nests.

    An original sequence of ``<svg:path .../>`` can otherwise become multiple
    opening path tags followed by closing tags at the end of the SVG. The XML
    remains well-formed, but readers render the graphic as a blank page.
    """
    if not re.search(r"</(?:svg:)?path\s*>", text, flags=re.IGNORECASE):
        return text

    def close_path(match: re.Match) -> str:
        tag = match.group(0)
        if tag.rstrip().endswith("/>"):
            return tag
        return tag[:-1].rstrip() + "/>"

    text = re.sub(
        r"<(?:svg:)?path\b[^>]*>",
        close_path,
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"</(?:svg:)?path\s*>", "", text, flags=re.IGNORECASE)


class EpubPackager:
    def __init__(self, book, output_path):
        self.book = book
        self.output_path = output_path

    def save(self):
        try:
            self._fix_toc_uids(self.book)
            self._ensure_epub3_navigation(self.book)
            self._ensure_navigation_targets_in_spine(self.book)
            epub.write_epub(self.output_path, self.book, {})
            self._post_fix()
            return True
        except Exception as e:
            print(f"Package Error: {e}")
            return False

    @staticmethod
    def _ensure_epub3_navigation(book) -> None:
        """ebooklib always writes EPUB 3, which requires exactly one nav item."""
        nav_items = [item for item in book.get_items() if isinstance(item, epub.EpubNav)]
        if not nav_items:
            book.add_item(epub.EpubNav())
            print("🔧 [PackageFix] Added missing EPUB 3 nav document")
        # ebooklib writes spine toc="ncx" even when no NCX item exists.
        # Supply a real compatibility NCX instead of a dangling OPF reference.
        if not any(isinstance(item, epub.EpubNcx) for item in book.get_items()):
            used_ids = {item.get_id() for item in book.get_items()}
            used_names = {item.get_name() for item in book.get_items()}
            uid, name = 'ncx', 'toc.ncx'
            while uid in used_ids: uid += '_compat'
            while name in used_names: name = 'compat_' + name
            book.add_item(epub.EpubNcx(uid=uid, file_name=name))

    @staticmethod
    def _ensure_navigation_targets_in_spine(book) -> None:
        """Make existing TOC/page-list targets reachable without changing flow.

        ebooklib regenerates a page-list from pagebreak markers in all HTML,
        including auxiliary documents excluded from the original spine.
        Preserve these references and append only existing HTML as linear=no;
        never fabricate a missing resource or insert it into normal reading.
        """
        present = set()
        for entry in book.spine:
            value = entry[0] if isinstance(entry, (tuple, list)) else entry
            present.add(value.get_id() if hasattr(value, 'get_id') else value)
        items = {posixpath.normpath(item.get_name()): item for item in book.get_items()
                 if isinstance(item, epub.EpubHtml)}
        added = 0

        def include(item):
            nonlocal added
            if item is not None and item.get_id() not in present:
                book.spine.append((item.get_id(), 'no'))
                present.add(item.get_id()); added += 1

        def visit(nodes):
            for node in nodes or []:
                obj = node[0] if isinstance(node, (tuple, list)) else node
                href = getattr(obj, 'href', None)
                if not href and isinstance(obj, epub.EpubHtml): href = obj.get_name()
                link = urlsplit(href or '')
                if link.path and not link.scheme and not link.netloc:
                    item = items.get(posixpath.normpath(unquote(link.path)))
                    include(item)
                if isinstance(node, (tuple, list)): visit(node[1])
        visit(book.toc)
        visit(getattr(book, 'pages', []))
        # ebooklib's book.pages can omit a retained EPUB3 page-list. The
        # original nav markup is still packaged, so include those actual
        # local HTML targets as non-linear auxiliary entries as well.
        for nav in items.values():
            if not isinstance(nav, epub.EpubNav):
                continue
            raw = nav.content or b''
            if isinstance(raw, str): raw = raw.encode('utf-8')
            if not raw: continue
            try:
                root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
            except etree.XMLSyntaxError:
                root = etree.fromstring(raw, etree.HTMLParser(no_network=True))
            if root is None: continue
            for href in root.xpath('//*[local-name()="nav"]//*[local-name()="a"]/@href'):
                link = urlsplit(href)
                if link.path and not link.scheme and not link.netloc:
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(nav.get_name()), unquote(link.path)))
                    include(items.get(target))
        # A source may have no page-list at all. Mirror the actual writer:
        # ebooklib 0.18 treats every epub:type + id as a page reference, not
        # only pagebreak. Checking only the latter leaves valid chapter/index
        # auxiliary targets outside the spine in the generated page-list.
        for item in items.values():
            if isinstance(item, epub.EpubNav) or item.get_id() in present: continue
            if item.content and get_pages(item):
                include(item)
                continue
            # Retain the legacy epub-type pagebreak compatibility path even
            # for caller-created items that have not yet been normalized.
            raw = item.content or b''
            if isinstance(raw, str): raw = raw.encode('utf-8')
            if b'epub-type' in raw:
                root = etree.fromstring(raw, etree.HTMLParser(no_network=True))
                if root is not None and any('pagebreak' in (node.get('epub-type') or '').split()
                                            for node in root.iter() if isinstance(node.tag, str)):
                    include(item)
        if added: print(f'🔧 [PackageFix] Added {added} non-linear navigation target(s)')

    @staticmethod
    def _fix_toc_uids(book) -> None:
        """ebooklib 在解析部分 EPUB 的 NCX 时不填 uid，写入时 lxml 会崩溃。
        遍历 toc，为所有 uid=None 的 Link/Section 自动补全 uid。"""
        counter = [0]

        def _fix(items):
            for item in items:
                if isinstance(item, tuple):
                    sec, children = item
                    if getattr(sec, "uid", None) is None:
                        counter[0] += 1
                        sec.uid = f"uid-sec-{counter[0]}"
                    _fix(children)
                else:
                    if getattr(item, "uid", None) is None:
                        counter[0] += 1
                        item.uid = f"uid-{counter[0]}"

        _fix(book.toc)
        if counter[0]:
            print(f"🔧 [PackageFix] Patched {counter[0]} TOC uid(s) (ebooklib NCX parse bug)")

    def _post_fix(self):
        """解压 -> 修复 ebooklib 引入的各种问题 -> 重新打包"""
        temp_dir = Path(tempfile.mkdtemp(prefix="epub_postfix_"))
        try:
            with zipfile.ZipFile(self.output_path, "r") as zf:
                zf.extractall(temp_dir)

            fixes_applied = []

            if self._preserve_navigation_documents(temp_dir):
                fixes_applied.append("original navigation structure")
            if self._sync_serialized_toc_files(temp_dir):
                fixes_applied.append("toc files")

            missing_font_sources = 0
            for css_path in temp_dir.rglob('*.css'):
                original_css = css_path.read_text(encoding='utf-8')
                repaired_css, count = repair_font_sources(original_css, css_path, temp_dir)
                if count:
                    css_path.write_text(repaired_css, encoding='utf-8')
                    missing_font_sources += count; fixes_applied.append(css_path.name)
            if missing_font_sources:
                print(f'⚠️ [FontFallback] Removed {missing_font_sources} absent optional font source(s); reader fallback retained')

            document_paths = [
                path
                for path in temp_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in {".xhtml", ".html", ".htm"}
            ]

            # Fix 1: SVG 大小写敏感属性与 ebooklib 丢失的命名空间
            for xhtml_path in document_paths:
                content = xhtml_path.read_text(encoding="utf-8", errors="ignore")
                original = content

                if "<svg" in content.lower() or "<image" in content.lower():
                    content = _fix_svg_attributes(content)
                    content = _fix_svg_path_serialization(content)
                content = _fix_missing_xml_namespaces(content)

                # Fix 2: ebooklib 清空了 <head>，补回 <title>
                if "<head/>" in content:
                    fname = xhtml_path.stem.replace("_", " ")
                    content = content.replace(
                        "<head/>",
                        f"<head><title>{fname}</title></head>",
                    )

                if content != original:
                    xhtml_path.write_text(content, encoding="utf-8")
                    fixes_applied.append(xhtml_path.name)

            # Fix 3: OPF - 修复 ibooks 前缀和 SVG 属性声明
            for opf_path in temp_dir.rglob("*.opf"):
                content = opf_path.read_text(encoding="utf-8", errors="ignore")
                original = content

                # 3a: 在 package 的 prefix 属性中注册 ibooks 前缀
                if 'ibooks:' in content and 'ibooks:' not in (
                    re.search(r'prefix="([^"]*)"', content) or type('', (), {'group': lambda s, x: ''})()
                ).group(1):
                    content = re.sub(
                        r'(prefix=")',
                        r'\1ibooks: http://vocabulary.itunes.apple.com/rdf/ibooks/vocabulary-extensions-1.0/ ',
                        content
                    )

                # 3b: 在含有 SVG 的 item 上声明 properties="svg"
                svg_files = set()
                for xhtml_path in document_paths:
                    xhtml_content = xhtml_path.read_text(encoding="utf-8", errors="ignore")
                    if "<svg" in xhtml_content.lower():
                        svg_files.add(xhtml_path.name)

                for svg_file in svg_files:
                    def add_svg_property(match):
                        tag = match.group(0)
                        if 'properties=' in tag:
                            return tag
                        # 在 /> 或 > 之前插入 properties="svg"
                        return re.sub(r'\s*/>', ' properties="svg"/>', tag)
                    content = re.sub(
                        rf'<item\b[^>]*href="[^"]*{re.escape(svg_file)}"[^>]*/?>',
                        add_svg_property,
                        content
                    )

                if content != original:
                    opf_path.write_text(content, encoding="utf-8")
                    fixes_applied.append(opf_path.name)

            if fixes_applied:
                print(f"🔧 [PostFix] Repaired {len(fixes_applied)} file(s): "
                      f"{', '.join(fixes_applied[:5])}{'...' if len(fixes_applied) > 5 else ''}")
                self._repack(temp_dir)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _navigation_types(node) -> set[str]:
        return set((node.get('{http://www.idpf.org/2007/ops}type') or '').split())

    def _preserve_navigation_documents(self, temp_dir: Path) -> bool:
        """ebooklib regenerates NAV and otherwise discards its non-TOC content.

        Retain the source document when its ordered destinations still match
        the authoritative TOC. This also preserves IDs that prose/guide links
        may reference. A changed TOC may be regenerated only if doing so does
        not silently discard nested anchors; ambiguous cases fail closed.
        """
        changed = False
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        for item in self.book.get_items():
            if not isinstance(item, epub.EpubNav) or not item.content:
                continue
            path = (temp_dir / self.book.FOLDER_NAME / item.get_name()).resolve()
            if not path.is_relative_to(temp_dir.resolve()) or not path.is_file():
                raise ValueError('Navigation output path is invalid')
            raw = item.content.encode('utf-8') if isinstance(item.content, str) else item.content
            original = etree.fromstring(raw, parser)
            if original.find('.//{*}body') is None:
                original = etree.fromstring(item.get_content(), parser)
            generated = etree.fromstring(path.read_bytes(), parser)
            body = original.find('.//{*}body')
            generated_body = generated.find('.//{*}body')
            if body is None or generated_body is None:
                raise ValueError('Navigation document has no body')
            old_tocs = [node for node in body.iter('{*}nav')
                        if 'toc' in self._navigation_types(node)]
            new_tocs = [node for node in generated_body.iter('{*}nav')
                        if 'toc' in self._navigation_types(node)]
            if len(old_tocs) > 1 or len(new_tocs) != 1:
                raise ValueError('Navigation TOC is ambiguous')
            new_toc = new_tocs[0]

            def entries(node):
                result = []
                for row in node.iter('{*}li'):
                    labels = [label for label in row.iterdescendants()
                              if isinstance(label.tag, str) and etree.QName(label).localname in {'a', 'span'}
                              and next(label.iterancestors('{*}li'), None) is row]
                    label = labels[0] if labels else None
                    href = label.get('href') if label is not None else None
                    uri = urlsplit(href or '')
                    destination = ((uri.scheme, uri.netloc,
                                    posixpath.normpath(unquote(uri.path)) if uri.path else '',
                                    uri.query, unquote(uri.fragment)) if href else None)
                    depth = sum(1 for ancestor in row.iterancestors('{*}li'))
                    result.append(((depth, destination, label is not None), label))
                return result

            def preserve_label_nodes(old, new):
                # Non-link section labels have no href for the later title
                # map. Synchronize their text too, without removing IDs.
                for (_key, old_label), (_new_key, new_label) in zip(old, new):
                    if old_label is None or new_label is None:
                        continue
                    title = ''.join(new_label.itertext())
                    if ''.join(old_label.itertext()) == title:
                        continue
                    texts = old_label.xpath('.//text()')
                    for index, text in enumerate(texts):
                        owner = text.getparent()
                        if text.is_tail:
                            owner.tail = title if index == 0 else ''
                        else:
                            owner.text = title if index == 0 else ''
                    if not texts:
                        old_label.text = title

            def safe_generated_copy(node, replacing=None):
                used = {element.get('id') for element in original.iter() if element.get('id')
                        and element is not replacing and (replacing is None or replacing not in element.iterancestors())}
                clone = deepcopy(node)
                remapped = {}
                for element in clone.iter():
                    old_id = element.get('id')
                    if not old_id:
                        continue
                    new_id = old_id
                    while new_id in used:
                        new_id += '-generated'
                    if new_id != old_id:
                        element.set('id', new_id)
                        remapped[old_id] = new_id
                    used.add(new_id)
                for element in clone.iter():
                    href = element.get('href', '')
                    if href.startswith('#') and unquote(href[1:]) in remapped:
                        element.set('href', '#' + quote(remapped[unquote(href[1:])], safe='-._~:'))
                    for attribute in ('aria-labelledby', 'aria-describedby'):
                        if element.get(attribute):
                            element.set(attribute, ' '.join(remapped.get(value, value)
                                                            for value in element.get(attribute).split()))
                return clone

            if old_tocs:
                old_toc = old_tocs[0]
                old_entries, new_entries = entries(old_toc), entries(new_toc)
                if [key for key, _ in old_entries] != [key for key, _ in new_entries]:
                    if any(node.get('id') for node in old_toc.iterdescendants()):
                        raise ValueError('Cannot regenerate TOC without losing existing navigation anchors')
                    replacement = safe_generated_copy(new_toc, replacing=old_toc)
                    for key, value in old_toc.attrib.items():
                        replacement.set(key, value)
                    old_toc.getparent().replace(old_toc, replacement)
                else:
                    preserve_label_nodes(old_entries, new_entries)
            else:
                body.append(safe_generated_copy(new_toc))

            # Keep source landmarks/page-list verbatim. Add generated auxiliary
            # navigation only when the source did not provide that type.
            existing_types = set().union(*(self._navigation_types(node)
                                           for node in body.iter('{*}nav')))
            for node in generated_body:
                if not isinstance(node.tag, str) or etree.QName(node).localname != 'nav':
                    continue
                types = self._navigation_types(node)
                if types and not types & existing_types:
                    body.append(safe_generated_copy(node))
                    existing_types.update(types)
            path.write_bytes(etree.tostring(original, encoding='utf-8', xml_declaration=True))
            changed = True
        return changed

    @staticmethod
    def _flatten_toc(items) -> list[tuple[str, str]]:
        """Return (href, title) pairs from ebooklib's mixed TOC structures."""
        pairs: list[tuple[str, str]] = []

        def walk(nodes) -> None:
            for node in nodes or []:
                if isinstance(node, tuple):
                    section, children = node
                    href = getattr(section, "href", None)
                    title = getattr(section, "title", None)
                    if href and title:
                        pairs.append((href, title))
                    walk(children)
                    continue
                href = getattr(node, "href", None)
                title = getattr(node, "title", None)
                if href and title:
                    pairs.append((href, title))

        walk(items)
        return pairs

    @staticmethod
    def _normalized_href_key(href: str) -> str:
        path, _fragment = urldefrag(unquote(href or ""))
        return path.replace("\\", "/").lstrip("./")

    @classmethod
    def _serialized_href_candidates(cls, temp_dir: Path, toc_file: Path, href: str) -> list[str]:
        if not href:
            return []
        parsed = urlparse(href)
        if parsed.scheme or parsed.netloc:
            return []

        href_path, fragment = urldefrag(unquote(href))
        normalized = cls._normalized_href_key(href)
        candidates = [f"{normalized}#{fragment}"] if fragment else []
        candidates.append(normalized)
        if fragment:
            candidates.append(cls._normalized_href_key(href_path))

        if href_path:
            try:
                toc_dir = toc_file.parent.relative_to(temp_dir)
            except ValueError:
                toc_dir = Path()
            resolved = (toc_dir / href_path).as_posix()
            resolved = cls._normalized_href_key(resolved)
            candidates.append(resolved)
            if fragment:
                candidates.insert(0, f"{resolved}#{fragment}")

        return list(dict.fromkeys(candidates))

    def _toc_title_map(self) -> dict[str, str]:
        title_candidates: dict[str, list[str]] = {}
        for href, title in self._flatten_toc(getattr(self.book, "toc", [])):
            text = str(title or "").strip()
            if not text:
                continue
            path, fragment = urldefrag(unquote(href or ""))
            path_key = self._normalized_href_key(path)
            exact_key = f"{path_key}#{fragment}" if fragment else path_key
            title_candidates.setdefault(exact_key, []).append(text)
            if fragment:
                title_candidates.setdefault(path_key, []).append(text)

        title_map: dict[str, str] = {}
        for key, titles in title_candidates.items():
            unique = list(dict.fromkeys(titles))
            if len(unique) == 1:
                title_map[key] = unique[0]
        return title_map

    def _sync_serialized_toc_files(self, temp_dir: Path) -> bool:
        """
        Keep physical nav.xhtml/toc.ncx labels aligned with book.toc.

        Some readers prefer the serialized navigation files over the in-memory
        TOC metadata written by ebooklib. After translation/rebuild, stale nav
        files can therefore show the original English directory.
        """
        title_map = self._toc_title_map()
        changed = False
        for nav_path in temp_dir.rglob("*.xhtml"):
            raw = nav_path.read_text(encoding="utf-8", errors="ignore")
            if "<nav" not in raw.lower():
                continue
            soup = BeautifulSoup(raw, "xml")
            local_changed = False
            toc_links = [a for nav in soup.find_all('nav')
                         if 'toc' in str(nav.get('epub:type') or '').split()
                         for a in nav.find_all('a', href=True)]
            for a in toc_links:
                candidates = self._serialized_href_candidates(temp_dir, nav_path, a.get("href", ""))
                title = next((title_map[c] for c in candidates if c in title_map), None)
                if title and a.get_text(strip=True) != title:
                    # Do not discard inline spans/IDs referenced by the book.
                    # Replace label text only; page-list and landmarks never
                    # receive chapter titles from this synchronization.
                    texts = [text for text in a.find_all(string=True) if not isinstance(text, Comment)]
                    if texts:
                        texts[0].replace_with(title)
                        for text in texts[1:]:
                            text.replace_with('')
                    else:
                        a.append(title)
                    local_changed = True
            if local_changed:
                nav_path.write_text(str(soup), encoding="utf-8")
                changed = True

        for ncx_path in temp_dir.rglob("*.ncx"):
            raw = ncx_path.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(raw, "xml")
            local_changed = False
            for nav_point in soup.find_all("navPoint"):
                content = nav_point.find("content")
                label_text = nav_point.find("text")
                if not content or not label_text:
                    continue
                # ebooklib writes NCX src from package-relative book.toc but
                # fails to relativize it when the NCX lives in a subdirectory.
                link = urlsplit(content.get('src', ''))
                if link.path and not link.scheme and not link.netloc:
                    package_dir = temp_dir / self.book.FOLDER_NAME
                    target = package_dir / unquote(link.path)
                    if target.is_file():
                        src = urlunsplit(('', '', quote(posixpath.relpath(target.as_posix(), ncx_path.parent.as_posix()), safe='/'), link.query, link.fragment))
                        if src != content.get('src'):
                            content['src'] = src; local_changed = True
                candidates = self._serialized_href_candidates(temp_dir, ncx_path, content.get("src", ""))
                title = next((title_map[c] for c in candidates if c in title_map), None)
                if title and label_text.get_text(strip=True) != title:
                    label_text.string = title
                    local_changed = True
            # NCX IDs have their own namespace: repairing invalid/duplicate
            # navPoint IDs must never rename document anchors or OPF item IDs.
            used_ids = set()
            for number, node in enumerate(soup.find_all(["navPoint", "pageTarget", "navTarget"])):
                uid = node.get('id') or ''
                try:
                    valid = bool(uid) and etree.QName(uid).namespace is None
                except ValueError:
                    valid = False
                if not valid or uid in used_ids:
                    uid = f'navpoint-compat-{number}'
                    while uid in used_ids: uid += '-compat'
                    node['id'] = uid; local_changed = True
                used_ids.add(uid)
            if local_changed:
                ncx_path.write_text(str(soup), encoding="utf-8")
                changed = True

        return changed

    def _repack(self, temp_dir: Path):
        """将修复后的目录重新打包为 EPUB"""
        output = Path(self.output_path)
        if output.exists():
            output.unlink()
        with zipfile.ZipFile(output, "w") as out:
            mimetype = temp_dir / "mimetype"
            if mimetype.exists():
                out.write(mimetype, "mimetype", compress_type=zipfile.ZIP_STORED)
            for file_path in temp_dir.rglob("*"):
                if file_path.is_file() and file_path.name != "mimetype":
                    out.write(
                        file_path,
                        file_path.relative_to(temp_dir).as_posix(),
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
