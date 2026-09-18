"""Offline source-resource warning and placeholder delivery regressions."""
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from ebooklib import epub

from app.converter import EpubConverter
from app.domain.fast_translation_runner import run_fast_translation_job
from app.domain.manifest_service import build_manifest
from app.domain.status_resolver import resolve_after_conversion
from app.domain.translation_attempt import restarted_translation_stats
from app.domain.translation_qa_service import audit_translated_epub_output, build_translation_qa_report
from app.engine.chunk_extractor import extract_chunks, is_source_placeholder_document
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.engine.compiler import ExtremeCompiler
from app.engine.glossary_service import GlossaryBuildResult
from app.engine.unpacker import EpubUnpacker
from app.models import ErrorCode, Job, JobStatus, OutputMode


PLACEHOLDER = ("<html><body data-epub-factory-missing-document='true' translate='no'>"
               "<section><h1>原文件缺少本章节</h1>"
               "<p>本页仅说明原文件缺失，不代表译文；其余可用章节继续处理。</p>"
               "</section></body></html>")


class SourceWarningFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.epub"
        self.output = self.root / "output.epub"
        book = epub.EpubBook()
        book.set_identifier("offline-source-warning")
        book.set_title("原书资源降级测试")
        book.set_language("zh")
        chapters = []
        for name, title in (("healthy.xhtml", "正常章节"), ("missing.xhtml", "缺失章节")):
            chapter = epub.EpubHtml(title=title, file_name=name, lang="zh")
            chapter.content = f"<html><body><h1>{title}</h1><p>这是可用章节的完整正文。</p></body></html>"
            book.add_item(chapter)
            chapters.append(chapter)
        book.spine = chapters
        book.toc = tuple(epub.Link(ch.get_name(), ch.title, ch.id) for ch in chapters)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        full = self.root / "full.epub"
        epub.write_epub(str(full), book)
        with zipfile.ZipFile(full) as source, zipfile.ZipFile(self.source, "w") as destination:
            for item in source.infolist():
                if not item.filename.endswith("/missing.xhtml"):
                    destination.writestr(item, source.read(item))

    def test_fixed_placeholder_has_no_chunks(self):
        self.assertTrue(is_source_placeholder_document(PLACEHOLDER))
        self.assertEqual(extract_chunks(PLACEHOLDER.encode(), "missing"), [])

    def test_marker_or_translate_attribute_cannot_hide_arbitrary_prose(self):
        for html in (
            '<html><body translate="no"><p>This actual source prose must still be translated.</p></body></html>',
            '<html><body data-epub-factory-missing-document="true"><p>This actual source prose must still be translated.</p></body></html>',
            PLACEHOLDER.replace('</section>', '<p>Real extra content still needs translation.</p></section>'),
        ):
            with self.subTest(html=html):
                self.assertFalse(is_source_placeholder_document(html))
                self.assertTrue(extract_chunks(html.encode(), "real"))

    def test_manifest_excludes_only_placeholder_and_preserves_source_warnings(self):
        manifest = build_manifest(str(self.source), "offline-warnings")
        self.assertNotIn("error", manifest)
        missing = next(ch for ch in manifest["chapters"] if ch["file_path"] == "missing.xhtml")
        healthy = next(ch for ch in manifest["chapters"] if ch["file_path"] == "healthy.xhtml")
        self.assertEqual(missing["chunks"], [])
        self.assertEqual(len(healthy["chunks"]), 2)
        self.assertEqual(manifest["stats"]["source_placeholder_documents_skipped"], 1)
        self.assertTrue(manifest["source_warnings"])

    def test_normal_conversion_preserves_warnings_after_repacking(self):
        with patch.object(ExtremeCompiler, "_run_epubcheck", return_value=True):
            result = EpubConverter().convert_file_to_horizontal(self.source, self.output, OutputMode.simplified)
        warnings = result.translation_stats["source_warnings"]
        self.assertTrue(warnings)
        self.assertTrue(result.validation_passed)
        self.assertIsNone(result.error_code)
        self.assertEqual(resolve_after_conversion(result)[0], JobStatus.success)
        self.assertTrue(all(message in result.message for message in warnings))
        unpacker = EpubUnpacker(str(self.output))
        self.assertIsNotNone(unpacker.load_book())
        self.assertEqual(unpacker.source_warnings, warnings)
        manifest = build_manifest(str(self.output), "repacked")
        self.assertEqual(manifest["source_warnings"], warnings)
        self.assertEqual(manifest["stats"]["source_placeholder_documents_skipped"], 1)

    def test_safe_mode_preserves_source_warning_without_faking_translation(self):
        compiler = ExtremeCompiler(str(self.source), str(self.output))
        with patch.object(compiler, "_run_full_pipeline", side_effect=RuntimeError("synthetic cleaner error")), \
             patch.object(compiler, "_run_epubcheck", return_value=True):
            self.assertTrue(compiler.run())
        self.assertTrue(compiler.validation_passed)
        self.assertEqual(compiler.metrics.mode, "safe")
        warnings = compiler.get_translation_stats()["source_warnings"]
        self.assertTrue(warnings)
        self.assertTrue(all(warning in compiler.final_message for warning in warnings))

    def test_ordinary_translation_never_sends_placeholder_to_translator_or_glossary(self):
        compiler = ExtremeCompiler(str(self.source), str(self.output), enable_translation=True)
        processed = []
        def process(translator, content, item_type):
            processed.append(content)
            translator.stats.total_chunks += 1
            translator.stats.translated_chunks += 1
            return content
        with patch.object(SemanticsTranslator, "process", new=process), \
             patch("app.engine.glossary_service.build_consistent_glossary", return_value=GlossaryBuildResult(glossary={})) as glossary, \
             patch.object(compiler, "_run_epubcheck", return_value=True):
            self.assertTrue(compiler.run())
        self.assertEqual(len(processed), 1)
        self.assertNotIn("原文件缺少本章节", processed[0].decode())
        self.assertNotIn("原文件缺少本章节", " ".join(glossary.call_args.args[0]))
        self.assertEqual(compiler.get_translation_stats()["total_chunks"], 1)
        self.assertTrue(compiler.get_translation_stats()["source_warnings"])

    def test_source_warnings_are_deliverable_and_not_failed_translation_flags(self):
        self.output.write_bytes(b"mock output exists")
        report = build_translation_qa_report(translation_stats={
            "source_warnings": ["原书缺少 1 个正文文件，已继续处理其余内容。"],
            "total_chunks": 2, "translated_chunks": 2,
            "artifact_audit": {"status": "passed", "residual_blocks": 0},
        }, output_path=self.output)
        self.assertEqual(report["status"], "warning")
        self.assertTrue(report["can_deliver"])
        self.assertEqual(report["flags"], [])
        self.assertEqual(report["score"], 100)
        self.assertFalse(report["retryable"])

    def test_retry_preserves_source_warnings_but_resets_model_counters(self):
        previous = {"source_warnings": ["原书缺少图片，已继续处理其余内容。"],
                    "translated_chunks": 7, "api_calls": 10}
        restarted = restarted_translation_stats(previous, attempt_id="new-attempt")
        self.assertEqual(restarted["source_warnings"], previous["source_warnings"])
        self.assertIsNot(restarted["source_warnings"], previous["source_warnings"])
        self.assertEqual(restarted["qa_report"]["source_warnings"], previous["source_warnings"])
        self.assertEqual(restarted["translated_chunks"], 0)
        self.assertEqual(restarted["api_calls"], 0)

    def test_source_warning_cannot_override_actual_translation_or_validation_failure(self):
        self.output.write_bytes(b"mock output exists")
        for error, extra in ((None, {"failed_chunks": 1}),
                             (ErrorCode.EPUB_VALIDATION_FAILED.value, {}),
                             (ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value, {}),
                             ("TRANSLATION_PROVIDER_UNAVAILABLE", {})):
            with self.subTest(error=error):
                report = build_translation_qa_report(translation_stats={
                    "source_warnings": ["原书缺少正文文件，已继续处理其余内容。"],
                    "artifact_audit": {"status": "passed"}, **extra,
                }, output_path=self.output, error_code=error)
                self.assertFalse(report["can_deliver"])
                self.assertIn(report["status"], {"failed", "blocked"})
                self.assertTrue(report["source_warnings"])

    def test_fast_translation_final_result_carries_warning_and_no_placeholder_chunk(self):
        job = Job(id="offline-fast-warnings", source_filename="synthetic.epub", trace_id="offline",
                  input_path=str(self.source), output_mode=OutputMode.simplified, enable_translation=True)
        async def translate(**kwargs):
            manifest = kwargs["manifest"]
            chunks = [chunk for chapter in manifest["chapters"]
                      if chapter["chapter_kind"] == "body" for chunk in chapter["chunks"]]
            self.assertEqual(len(chunks), 2)
            self.assertFalse(any("原文件缺少本章节" in chunk["text"] for chunk in chunks))
            return {"total_chunks": 2, "translated_chunks": 2,
                    "artifact_audit": {"status": "passed", "residual_blocks": 0}}, {}
        with patch.dict(os.environ, {"EPUB_TRANSLATION_CHECKPOINT_DB": str(self.root / "checkpoints.db")}), \
             patch.object(ExtremeCompiler, "_run_epubcheck", return_value=True), \
             patch("app.domain.fast_translation_runner.profile_book", return_value={"status": "ok", "genre": "nonfiction"}), \
             patch("app.domain.fast_translation_runner.build_consistent_glossary", return_value=GlossaryBuildResult(glossary={})), \
             patch("app.domain.fast_translation_runner._translate_book_title_async", new=AsyncMock(return_value="原书资源降级测试译文")), \
             patch("app.domain.fast_translation_runner._translate_manifest_async", new=translate), \
             patch("app.domain.fast_translation_runner.job_store", new=Mock()), \
             patch("app.domain.fast_translation_runner.make_get_chapter_content", return_value=lambda _: None), \
             patch("app.domain.fast_translation_runner._run_epubcheck", return_value=True):
            result = run_fast_translation_job(job=job, input_path=self.source, output_path=self.output,
                                             progress_callback=Mock(), stage_callback=Mock())
        self.assertTrue(result.validation_passed)
        self.assertIsNone(result.error_code)
        warnings = result.translation_stats["source_warnings"]
        self.assertTrue(warnings)
        self.assertTrue(all(warning in result.message for warning in warnings))
        self.assertEqual(result.translation_stats["total_chunks"], 2)
        self.assertEqual(result.translation_stats["source_placeholder_documents_skipped"], 1)
        qa = result.translation_stats["qa_report"]
        self.assertEqual(qa["status"], "warning")
        self.assertTrue(qa["can_deliver"])
        audit = audit_translated_epub_output(self.output)
        self.assertEqual(audit["source_placeholder_documents_skipped"], 1)
        self.assertEqual(audit["residual_blocks"], 0)


if __name__ == "__main__":
    unittest.main()
