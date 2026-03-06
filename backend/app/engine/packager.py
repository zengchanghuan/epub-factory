import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import ebooklib
from ebooklib import epub

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
            epub.write_epub(self.output_path, self.book, {})
            self._post_fix()
            return True
        except Exception as e:
            print(f"Package Error: {e}")
            return False

    def _post_fix(self):
        """解压 -> 修复 ebooklib 引入的各种问题 -> 重新打包"""
        temp_dir = Path(tempfile.mkdtemp(prefix="epub_postfix_"))
        try:
            with zipfile.ZipFile(self.output_path, "r") as zf:
                zf.extractall(temp_dir)

            fixes_applied = []

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