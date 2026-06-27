import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urldefrag, urlparse

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

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


def _fix_svg_attributes(text: str) -> str:
    for wrong, correct in SVG_CASE_FIXES.items():
        text = re.sub(
            rf'\b{wrong}=',
            f'{correct}=',
            text
        )
    return text


class EpubPackager:
    def __init__(self, book, output_path):
        self.book = book
        self.output_path = output_path

    def save(self):
        try:
            self._fix_toc_uids(self.book)
            epub.write_epub(self.output_path, self.book, {})
            self._post_fix()
            return True
        except Exception as e:
            print(f"Package Error: {e}")
            return False

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

            if self._sync_serialized_toc_files(temp_dir):
                fixes_applied.append("toc files")

            # Fix 1: SVG 大小写敏感属性
            for xhtml_path in temp_dir.rglob("*.xhtml"):
                content = xhtml_path.read_text(encoding="utf-8", errors="ignore")
                original = content

                if '<svg' in content.lower() or '<image' in content.lower():
                    content = _fix_svg_attributes(content)

                # Fix 2: ebooklib 清空了 <head>，补回 <title>
                if '<head/>' in content:
                    fname = xhtml_path.stem.replace('_', ' ')
                    content = content.replace(
                        '<head/>',
                        f'<head><title>{fname}</title></head>'
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
                for xhtml_path in temp_dir.rglob("*.xhtml"):
                    xhtml_content = xhtml_path.read_text(encoding="utf-8", errors="ignore")
                    if '<svg' in xhtml_content.lower():
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
        candidates = [normalized]
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
        title_map: dict[str, str] = {}
        for href, title in self._flatten_toc(getattr(self.book, "toc", [])):
            text = str(title or "").strip()
            if not text:
                continue
            title_map[self._normalized_href_key(href)] = text
            path, fragment = urldefrag(unquote(href or ""))
            if fragment:
                title_map[f"{self._normalized_href_key(path)}#{fragment}"] = text
        return title_map

    def _sync_serialized_toc_files(self, temp_dir: Path) -> bool:
        """
        Keep physical nav.xhtml/toc.ncx labels aligned with book.toc.

        Some readers prefer the serialized navigation files over the in-memory
        TOC metadata written by ebooklib. After translation/rebuild, stale nav
        files can therefore show the original English directory.
        """
        title_map = self._toc_title_map()
        if not title_map:
            return False

        changed = False
        for nav_path in temp_dir.rglob("*.xhtml"):
            raw = nav_path.read_text(encoding="utf-8", errors="ignore")
            if "<nav" not in raw.lower():
                continue
            soup = BeautifulSoup(raw, "xml")
            local_changed = False
            for a in soup.find_all("a", href=True):
                candidates = self._serialized_href_candidates(temp_dir, nav_path, a.get("href", ""))
                title = next((title_map[c] for c in candidates if c in title_map), None)
                if title and a.get_text(strip=True) != title:
                    a.clear()
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
                candidates = self._serialized_href_candidates(temp_dir, ncx_path, content.get("src", ""))
                title = next((title_map[c] for c in candidates if c in title_map), None)
                if title and label_text.get_text(strip=True) != title:
                    label_text.string = title
                    local_changed = True
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
