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
            self._post_fix_svg()
            return True
        except Exception as e:
            print(f"Package Error: {e}")
            return False

    def _post_fix_svg(self):
        """解压 -> 修复 SVG 属性大小写 -> 重新打包"""
        temp_dir = Path(tempfile.mkdtemp(prefix="epub_svgfix_"))
        try:
            with zipfile.ZipFile(self.output_path, "r") as zf:
                zf.extractall(temp_dir)

            fixed_count = 0
            for xhtml_path in temp_dir.rglob("*.xhtml"):
                content = xhtml_path.read_text(encoding="utf-8", errors="ignore")
                if '<svg' in content.lower() or '<image' in content.lower():
                    fixed = _fix_svg_attributes(content)
                    if fixed != content:
                        xhtml_path.write_text(fixed, encoding="utf-8")
                        fixed_count += 1

            if fixed_count > 0:
                print(f"🔧 [SVG Fix] Repaired case-sensitive attributes in {fixed_count} file(s)")
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
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)