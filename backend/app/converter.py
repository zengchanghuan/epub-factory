import html
import shutil
import tempfile
import zipfile
from pathlib import Path

from .models import OutputMode
from .engine.compiler import ExtremeCompiler


class EpubConverter:
    def __init__(self) -> None:
        pass

    def convert_file_to_horizontal(
        self,
        input_path: Path,
        output_path: Path,
        output_mode: OutputMode,
    ) -> None:
        suffix = input_path.suffix.lower()
        if suffix == ".epub":
            self._convert_epub_to_horizontal(input_path, output_path, output_mode)
            return
        if suffix == ".pdf":
            self._convert_pdf_to_horizontal_epub(input_path, output_path, output_mode)
            return
        raise RuntimeError("Unsupported file type, only .epub or .pdf is allowed")

    def _convert_epub_to_horizontal(
        self,
        input_path: Path,
        output_path: Path,
        output_mode: OutputMode,
    ) -> None:
        compiler = ExtremeCompiler(
            input_path=str(input_path),
            output_path=str(output_path),
            output_mode=output_mode.value
        )
        success = compiler.run()
        if not success:
            raise RuntimeError("ExtremeCompiler failed to convert EPUB")

    def _convert_pdf_to_horizontal_epub(
        self,
        input_path: Path,
        output_path: Path,
        output_mode: OutputMode,
    ) -> None:
        temp_epub = Path(tempfile.mkdtemp(prefix="epub_factory_pdf_")) / "source.epub"
        try:
            self._pdf_to_epub(input_path, temp_epub)
            self._convert_epub_to_horizontal(temp_epub, output_path, output_mode)
        finally:
            shutil.rmtree(temp_epub.parent, ignore_errors=True)

    def _pdf_to_epub(self, input_pdf: Path, output_epub: Path) -> None:
        try:
            from pypdf import PdfReader
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PDF conversion dependency missing: please install `pypdf` in current venv."
            ) from exc

        reader = PdfReader(str(input_pdf))
        paragraphs: list[str] = []
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            if text:
                paragraphs.extend([line.strip() for line in text.splitlines() if line.strip()])

        if not paragraphs:
            paragraphs = ["该 PDF 未提取到可读文本，已生成占位内容。"]

        body = "\n".join(f"<p>{html.escape(line)}</p>" for line in paragraphs)
        chapter_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-Hant" lang="zh-Hant">
  <head>
    <meta charset="utf-8"/>
    <title>Converted PDF</title>
    <style>
      html, body {{
        writing-mode: horizontal-tb;
        line-height: 1.8;
        margin: 1rem;
      }}
    </style>
  </head>
  <body>
{body}
  </body>
</html>
"""
        container_xml = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
        content_opf = """<?xml version="1.0" encoding="utf-8"?>
<package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookId">epub-factory-pdf</dc:identifier>
    <dc:title>Converted PDF</dc:title>
    <dc:language>zh-Hant</dc:language>
  </metadata>
  <manifest>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine page-progression-direction="ltr">
    <itemref idref="chapter1"/>
  </spine>
</package>
"""

        output_epub.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_epub, "w") as out:
            out.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            out.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)
            out.writestr("OEBPS/content.opf", content_opf, compress_type=zipfile.ZIP_DEFLATED)
            out.writestr("OEBPS/chapter1.xhtml", chapter_xhtml, compress_type=zipfile.ZIP_DEFLATED)


converter = EpubConverter()
