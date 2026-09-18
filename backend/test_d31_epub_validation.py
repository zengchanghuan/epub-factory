"""Offline delivery-gate regressions; no broker, network or model requests."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ebooklib import epub

from app.domain.fast_translation_runner import _run_epubcheck, run_fast_translation_job
from app.domain.status_resolver import resolve_after_conversion
from app.domain.translation_qa_service import build_translation_qa_report
from app.engine.compiler import ExtremeCompiler
from app.engine.epub_validation import validate_epub
from app.engine.glossary_service import GlossaryBuildResult
from app.models import ConversionResult, DeviceProfile, ErrorCode, Job, JobStatus, OutputMode


def report(*severities):
    return {
        "messages": [{"severity": severity} for severity in severities],
        "checker": {"nFatal": severities.count("FATAL"), "nError": severities.count("ERROR"),
                    "nWarning": severities.count("WARNING")},
    }


class EpubValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output.epub"
        self.output.write_bytes(b"synthetic-output")
        self.jar = self.root / "epubcheck.jar"
        self.jar.touch()
        self.report_paths = []

    def execute(self, payload, returncode=0):
        def run(command, **kwargs):
            destination = Path(command[-1])
            self.report_paths.append(destination)
            if payload is not None:
                destination.write_text(payload if isinstance(payload, str) else json.dumps(payload))
            return subprocess.CompletedProcess(command, returncode, "", "")
        with patch("app.engine.epub_validation.subprocess.run", side_effect=run):
            result = validate_epub(self.output, self.jar)
        self.assertTrue(self.report_paths)
        self.assertTrue(all(not path.parent.exists() for path in self.report_paths))
        return result

    def assert_unavailable(self, result):
        self.assertFalse(result.passed)
        self.assertEqual(result.error_code, ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value)

    def test_valid_report_passes(self):
        result = self.execute(report())
        self.assertTrue(result.passed)
        self.assertIsNone(result.error_code)

    def test_warning_only_report_is_deliverable(self):
        result = self.execute(report("WARNING", "USAGE"))
        self.assertTrue(result.passed)
        self.assertEqual(result.warnings, 1)

    def test_fatal_and_error_reports_are_publication_failures(self):
        for severity in ("ERROR", "FATAL"):
            with self.subTest(severity=severity):
                result = self.execute(report(severity), returncode=1)
                self.assertFalse(result.passed)
                self.assertEqual(result.error_code, ErrorCode.EPUB_VALIDATION_FAILED.value)

    def test_zero_exit_cannot_override_error_findings(self):
        self.assertFalse(self.execute(report("ERROR")).passed)

    def test_nonzero_exit_cannot_override_clean_report(self):
        self.assert_unavailable(self.execute(report(), returncode=2))

    def test_missing_jar_never_runs_process(self):
        with patch("app.engine.epub_validation.subprocess.run") as run:
            result = validate_epub(self.output, self.root / "missing.jar")
        self.assert_unavailable(result)
        run.assert_not_called()

    def test_missing_output_is_not_deliverable(self):
        with patch("app.engine.epub_validation.subprocess.run") as run:
            result = validate_epub(self.root / "absent.epub", self.jar)
        self.assertFalse(result.passed)
        self.assertEqual(result.error_code, ErrorCode.EPUB_VALIDATION_FAILED.value)
        run.assert_not_called()

    def test_missing_java_is_unavailable_and_cleans_temporary_directory(self):
        def run(command, **kwargs):
            self.report_paths.append(Path(command[-1]))
            raise FileNotFoundError("java")
        with patch("app.engine.epub_validation.subprocess.run", side_effect=run):
            result = validate_epub(self.output, self.jar)
        self.assert_unavailable(result)
        self.assertTrue(all(not path.parent.exists() for path in self.report_paths))

    def test_timeout_is_unavailable_and_cleans_partial_report(self):
        def run(command, **kwargs):
            destination = Path(command[-1])
            self.report_paths.append(destination)
            destination.write_text("unfinished")
            raise subprocess.TimeoutExpired(command, 60)
        with patch("app.engine.epub_validation.subprocess.run", side_effect=run):
            result = validate_epub(self.output, self.jar)
        self.assert_unavailable(result)
        self.assertTrue(all(not path.parent.exists() for path in self.report_paths))

    def test_missing_or_invalid_json_is_unavailable(self):
        for payload in (None, "", "invalid", "null", "[]", "{}"):
            with self.subTest(payload=payload):
                self.assert_unavailable(self.execute(payload))

    def test_malformed_messages_or_summary_cannot_claim_success(self):
        for payload in (
            {"messages": []},
            {"messages": {}, "checker": report()["checker"]},
            {"messages": [None], "checker": report()["checker"]},
            report("UNKNOWN"),
            {"messages": [], "checker": {"nFatal": 0, "nError": "0", "nWarning": 0}},
            {"messages": [], "checker": {"nFatal": 0, "nError": False, "nWarning": 0}},
            {"messages": [], "checker": {"nFatal": 0, "nError": 1, "nWarning": 0}},
        ):
            with self.subTest(payload=payload):
                self.assert_unavailable(self.execute(payload))

    def test_both_existing_boolean_interfaces_fail_closed(self):
        compiler = ExtremeCompiler(str(self.output), str(self.output))
        self.assertFalse(compiler.validation_passed)
        with patch("app.engine.compiler.EPUBCHECK_JAR", str(self.root / "absent.jar")):
            self.assertFalse(compiler._run_epubcheck())
        self.assertEqual(compiler._epubcheck_result.error_code, ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value)
        results = []
        with patch("app.domain.fast_translation_runner.EPUBCHECK_JAR", str(self.root / "absent.jar")):
            self.assertFalse(_run_epubcheck(self.output, on_result=results.append))
        self.assertEqual(results[0].error_code, ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value)

    def test_ordinary_pipeline_preserves_infrastructure_failure_reason(self):
        self.make_book()
        output = self.root / "converted.epub"
        compiler = ExtremeCompiler(str(self.output), str(output))
        with patch("app.engine.compiler.EPUBCHECK_JAR", str(self.root / "absent.jar")):
            self.assertTrue(compiler.run())  # File generation and deliverability are separate.
        self.assertTrue(output.is_file())
        self.assertFalse(compiler.validation_passed)
        self.assertIn("缺少 EPUBCheck", compiler.final_message)
        status, _, code = resolve_after_conversion(ConversionResult(
            validation_passed=compiler.validation_passed,
            message=compiler.final_message, error_code=compiler.error_code,
        ))
        self.assertEqual(status, JobStatus.failed)
        self.assertEqual(code, ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value)

    def test_safe_fallback_requires_actual_validation(self):
        self.make_book()
        compiler = ExtremeCompiler(str(self.output), str(self.root / "safe.epub"))
        with patch.object(compiler, "_run_full_pipeline", side_effect=RuntimeError("synthetic cleaner error")), \
             patch("app.engine.compiler.EPUBCHECK_JAR", str(self.root / "absent.jar")):
            self.assertTrue(compiler.run())
        self.assertEqual(compiler.metrics.mode, "safe")
        self.assertFalse(compiler.validation_passed)
        self.assertEqual(compiler.error_code, ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value)
        self.assertTrue(any(stage.name == "EpubCheck" and stage.status == "error" for stage in compiler.metrics.stages))

    def test_safe_fallback_accepts_successful_validator_report(self):
        self.make_book()
        compiler = ExtremeCompiler(str(self.output), str(self.root / "safe-valid.epub"))
        def run(command, **kwargs):
            self.assertTrue(Path(command[3]).is_file())
            Path(command[-1]).write_text(json.dumps(report("WARNING")))
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(compiler, "_run_full_pipeline", side_effect=RuntimeError("synthetic cleaner error")), \
             patch("app.engine.compiler.EPUBCHECK_JAR", str(self.jar)), \
             patch("app.engine.epub_validation.subprocess.run", side_effect=run):
            self.assertTrue(compiler.run())
        self.assertTrue(compiler.validation_passed)

    def test_fast_preprocessing_failure_stops_before_model_preparation(self):
        job = SimpleNamespace(id="offline-validation", trace_id="offline", output_mode=OutputMode.simplified,
                              target_lang="zh-CN", device=DeviceProfile.generic)
        rejected = ConversionResult(validation_passed=False,
            message="无法完成 EPUB 校验：服务器缺少 Java 运行环境，结果不可交付",
            error_code=ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value)
        with patch("app.domain.fast_translation_runner.converter.convert_file_to_horizontal", return_value=rejected), \
             patch("app.domain.fast_translation_runner.build_manifest") as manifest, \
             patch("app.domain.fast_translation_runner.profile_book") as profile, \
             patch("app.domain.fast_translation_runner.build_consistent_glossary") as glossary, \
             patch("app.domain.fast_translation_runner._translate_book_title_async") as title, \
             patch("app.domain.fast_translation_runner._translate_manifest_async") as translate:
            result = run_fast_translation_job(job=job, input_path=self.output, output_path=self.root / "unused.epub",
                                             progress_callback=Mock(), stage_callback=Mock())
        self.assertIs(result, rejected)
        for fn in (manifest, profile, glossary, title, translate):
            fn.assert_not_called()
        self.assertFalse((self.root / "unused.epub").exists())

    def test_text_qa_cannot_overrule_failed_or_unavailable_format_validation(self):
        for error_code, expected in ((ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value, "blocked"),
                                     (ErrorCode.EPUB_VALIDATION_FAILED.value, "failed")):
            with self.subTest(error_code=error_code):
                result = build_translation_qa_report(
                    translation_stats={"total_chunks": 1, "translated_chunks": 1,
                     "artifact_audit": {"status": "passed", "residual_blocks": 0}},
                    output_path=self.output, error_code=error_code,
                )
                self.assertFalse(result["can_deliver"])
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["delivery_status"], expected)
                self.assertFalse(result["retryable"])

    def test_fast_final_validation_failure_reaches_status_and_qa(self):
        self.make_book()
        job = Job(id="offline-final-validation", source_filename="synthetic.epub", trace_id="offline",
                  input_path=str(self.output), output_mode=OutputMode.simplified, enable_translation=True)
        output = self.root / "translated.epub"
        def preprocess(source, destination, *args, **kwargs):
            shutil.copyfile(source, destination)
            return ConversionResult(validation_passed=True)
        with patch.dict(os.environ, {"EPUB_TRANSLATION_CHECKPOINT_DB": str(self.root / "checkpoints.db")}), \
             patch("app.domain.fast_translation_runner.converter.convert_file_to_horizontal", side_effect=preprocess), \
             patch("app.domain.fast_translation_runner.profile_book", return_value={"status": "ok", "genre": "nonfiction"}), \
             patch("app.domain.fast_translation_runner.build_consistent_glossary", return_value=GlossaryBuildResult(glossary={})), \
             patch("app.domain.fast_translation_runner._translate_book_title_async", new=AsyncMock(return_value="合成校验用例")), \
             patch("app.domain.fast_translation_runner._translate_manifest_async", new=AsyncMock(return_value=(
                 {"total_chunks": 1, "translated_chunks": 1}, {}))), \
             patch("app.domain.fast_translation_runner.job_store", new=Mock()), \
             patch("app.domain.fast_translation_runner.make_get_chapter_content", return_value=lambda _: None), \
             patch("app.domain.fast_translation_runner.EPUBCHECK_JAR", str(self.root / "missing-after-preprocess.jar")):
            result = run_fast_translation_job(job=job, input_path=self.output, output_path=output,
                                             progress_callback=Mock(), stage_callback=Mock())
        self.assertTrue(output.is_file())
        self.assertFalse(result.validation_passed)
        self.assertEqual(result.error_code, ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value)
        self.assertIn("缺少 EPUBCheck", result.message)
        self.assertEqual(resolve_after_conversion(result)[0], JobStatus.failed)
        self.assertFalse(result.translation_stats["qa_report"]["can_deliver"])
        self.assertEqual(result.translation_stats["qa_report"]["status"], "blocked")
        self.assertIn("❌ EpubCheck", result.metrics_summary)

    def make_book(self):
        book = epub.EpubBook()
        book.set_identifier("offline-validation")
        book.set_title("合成校验用例")
        book.set_language("zh")
        chapter = epub.EpubHtml(title="正文", file_name="chapter.xhtml", lang="zh")
        chapter.content = "<html><body><p>这是不含用户书籍的合成正文。</p></body></html>"
        book.add_item(chapter)
        book.spine = [chapter]
        book.toc = (epub.Link("chapter.xhtml", "正文", "chapter"),)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(str(self.output), book)


if __name__ == "__main__":
    unittest.main()
