import html as _html_mod
import shutil
import tempfile
from pathlib import Path

from .models import ConversionResult, DeviceProfile, OutputMode
from .engine import ExtremeCompiler
from .engine.adapters import html_to_epub_builder


class EpubConverter:
    def __init__(self) -> None:
        pass

    def convert_file_to_horizontal(
        self,
        input_path: Path,
        output_path: Path,
        output_mode: OutputMode,
        enable_translation: bool = False,
        target_lang: str = "zh-CN",
        device: str = "generic",
        bilingual: bool = False,
        glossary: dict | None = None,
        temperature: float | None = None,
        traditional_variant: str = "auto",
        lexicon_domains: list | None = None,
        enable_proper_noun: bool = True,
        progress_callback=None,
        stage_callback=None,
    ) -> ConversionResult:
        suffix = input_path.suffix.lower()
        if suffix == ".epub":
            return self._convert_epub_to_horizontal(
                input_path, output_path, output_mode, enable_translation, target_lang,
                device, bilingual, glossary, temperature, traditional_variant,
                lexicon_domains, enable_proper_noun, progress_callback, stage_callback,
            )
        if suffix == ".pdf":
            return self._convert_via_html_to_epub(
                input_path, output_path, output_mode, enable_translation, target_lang,
                device, bilingual, glossary, temperature, traditional_variant,
                lexicon_domains, enable_proper_noun, progress_callback, stage_callback,
                adapter="pdf",
            )
        if suffix == ".docx":
            return self._convert_via_html_to_epub(
                input_path, output_path, output_mode, enable_translation, target_lang,
                device, bilingual, glossary, temperature, traditional_variant,
                lexicon_domains, enable_proper_noun, progress_callback, stage_callback,
                adapter="docx",
            )
        if suffix in (".md", ".markdown"):
            return self._convert_via_html_to_epub(
                input_path, output_path, output_mode, enable_translation, target_lang,
                device, bilingual, glossary, temperature, traditional_variant,
                lexicon_domains, enable_proper_noun, progress_callback, stage_callback,
                adapter="markdown",
            )
        raise RuntimeError(
            f"Unsupported file type '{suffix}'. "
            "Supported: .epub, .pdf, .docx, .md, .markdown"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Core EPUB → EPUB pipeline
    # ──────────────────────────────────────────────────────────────────────

    def _convert_epub_to_horizontal(
        self,
        input_path: Path,
        output_path: Path,
        output_mode: OutputMode,
        enable_translation: bool = False,
        target_lang: str = "zh-CN",
        device: str = "generic",
        bilingual: bool = False,
        glossary: dict | None = None,
        temperature: float | None = None,
        traditional_variant: str = "auto",
        lexicon_domains: list | None = None,
        enable_proper_noun: bool = True,
        progress_callback=None,
        stage_callback=None,
    ) -> ConversionResult:
        from .models import LexiconStats
        compiler = ExtremeCompiler(
            input_path=str(input_path),
            output_path=str(output_path),
            output_mode=output_mode.value,
            enable_translation=enable_translation,
            target_lang=target_lang,
            device=device,
            bilingual=bilingual,
            glossary=glossary,
            temperature=temperature,
            traditional_variant=traditional_variant,
            lexicon_domains=lexicon_domains,
            enable_proper_noun=enable_proper_noun,
            progress_callback=progress_callback,
            stage_callback=stage_callback,
        )
        success = compiler.run()
        if not success:
            raise RuntimeError(compiler.final_message or "ExtremeCompiler failed to convert EPUB")

        lexicon_stats = LexiconStats.empty()
        try:
            lx_report = compiler._cjk_normalizer.get_report()
            if lx_report:
                lexicon_stats = LexiconStats(
                    versions=lx_report.versions,
                    total_replacements=lx_report.total_replacements,
                    top_hits=[
                        {"layer": h.layer, "tw": h.tw, "cn": h.cn, "count": h.count, "domain": h.domain}
                        for h in lx_report.hits
                    ],
                )
        except Exception:
            pass

        return ConversionResult(
            quality_stats=compiler.job_stats,
            translation_stats=compiler.get_translation_stats(),
            lexicon_stats=lexicon_stats,
            metrics_summary=compiler.metrics.summary(),
            message=compiler.final_message or "转换成功",
            error_code=compiler.error_code,
            validation_passed=getattr(compiler, "validation_passed", True),
        )

    # ──────────────────────────────────────────────────────────────────────
    # Generic adapter path: source → HTML → minimal EPUB → ExtremeCompiler
    # ──────────────────────────────────────────────────────────────────────

    def _convert_via_html_to_epub(
        self,
        input_path: Path,
        output_path: Path,
        output_mode: OutputMode,
        enable_translation: bool = False,
        target_lang: str = "zh-CN",
        device: str = "generic",
        bilingual: bool = False,
        glossary: dict | None = None,
        temperature: float | None = None,
        traditional_variant: str = "auto",
        lexicon_domains: list | None = None,
        enable_proper_noun: bool = True,
        progress_callback=None,
        stage_callback=None,
        adapter: str = "pdf",
    ) -> ConversionResult:
        html_body, metadata = self._run_adapter(input_path, adapter)

        tmp_dir = Path(tempfile.mkdtemp(prefix=f"epub_factory_{adapter}_"))
        temp_epub = tmp_dir / "source.epub"
        try:
            html_to_epub_builder.build(html_body, metadata, temp_epub)
            return self._convert_epub_to_horizontal(
                temp_epub, output_path, output_mode, enable_translation, target_lang,
                device, bilingual, glossary, temperature, traditional_variant,
                lexicon_domains, enable_proper_noun, progress_callback, stage_callback,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _run_adapter(input_path: Path, adapter: str) -> tuple[str, dict]:
        """Dispatch to the correct format adapter and return (html_body, metadata)."""
        if adapter == "pdf":
            return _pdf_to_html(input_path)
        if adapter == "docx":
            from .engine.adapters.docx_adapter import docx_to_html
            return docx_to_html(input_path)
        if adapter == "markdown":
            from .engine.adapters.markdown_adapter import md_to_html
            return md_to_html(input_path)
        raise RuntimeError(f"Unknown adapter: {adapter}")


# ──────────────────────────────────────────────────────────────────────────
# PDF → HTML helper (kept here; no new dependency needed)
# ──────────────────────────────────────────────────────────────────────────

def _pdf_to_html(input_pdf: Path) -> tuple[str, dict]:
    """Extract plain text from a PDF and return as HTML paragraphs."""
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

    html_body = "\n".join(f"<p>{_html_mod.escape(line)}</p>" for line in paragraphs)
    metadata = {
        "title": input_pdf.stem,
        "author": "",
        "language": "zh",
        "identifier": "",
    }
    return html_body, metadata


converter = EpubConverter()
