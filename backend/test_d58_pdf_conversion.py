"""Isolation/publication contracts. PDF/model/payment transports are not used."""
from dataclasses import replace
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from app.domain import pdf_conversion as conversion


def receipt(source, epub):
    text = "Synthetic source"
    digest = hashlib.sha256(text.encode()).hexdigest()
    return {"schema_version": conversion.SCHEMA,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "output_sha256": hashlib.sha256(epub.read_bytes()).hexdigest(),
            "memory_limited": True, "page_count": 1, "normalized_characters": 15,
            "zero_width_spaces_preserved": 0, "image_assets": 0, "image_placements": 0,
            "toc_entries": 0, "paragraph_count": 1, "warnings": [],
            "pages": [{"number": 1, "text_sha256": digest, "normalized_sha256": digest,
                       "char_count": 16, "normalized_char_count": 15, "image_placements": 0}]}


class Done:
    returncode = 0
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        return self.returncode


class PdfConversionContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pdf_conversion_contract_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.output = self.root / "source.pdf", self.root / "result.epub"
        self.source.write_bytes(b"%PDF-1.4\nSYNTHETIC_ONLY")
        self.original = self.source.read_bytes()
        self.launches = []
        self.transform = lambda value: value
        self.validation = SimpleNamespace(passed=True, warnings=0, error_code=None)
        self.validation_calls = []

    def launch(self, args, **kwargs):
        self.launches.append((args, kwargs))
        source, epub, result = map(Path, args[5:8])
        epub.write_bytes(b"synthetic-epub-transport-only")
        result.write_text(json.dumps(self.transform(receipt(source, epub))))
        return Done()

    def validate(self, path, jar, **kwargs):
        self.validation_calls.append((path, jar))
        return self.validation

    def convert(self, **kwargs):
        with patch.object(conversion.subprocess, "Popen", self.launch), patch.object(
                conversion, "_validate", self.validate):
            return conversion.convert_text_pdf(self.source, self.output, **kwargs)

    def assert_reason(self, reason, **kwargs):
        with self.assertRaises(conversion.PdfConversionError) as error:
            self.convert(**kwargs)
        self.assertEqual(error.exception.reason, reason)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_receipt_and_private_no_clobber_publication(self):
        result = self.convert()
        self.assertTrue(result["validation_passed"])
        self.assertTrue(result["eligible_for_payment"])
        self.assertEqual(result["output_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(any(p.name.startswith(".pdf-publish-") for p in self.root.iterdir()))

    def test_worker_has_no_config_or_secrets(self):
        with patch.dict(os.environ, {"SECRET_SENTINEL": "not-forwarded", "DEEPSEEK_API_KEY": "not-forwarded"}):
            self.convert()
        args, options = self.launches[0]
        self.assertEqual(args[1:3], ["-I", "-B"])
        self.assertTrue(options["start_new_session"])
        self.assertEqual(set(options["env"]), {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL"})
        self.assertNotIn("not-forwarded", json.dumps(options))
        self.assertFalse(Path(options["cwd"]).exists())

    def test_existing_output_never_overwritten(self):
        self.output.write_bytes(b"existing user file")
        with self.assertRaises(conversion.PdfConversionError) as error:
            self.convert()
        self.assertEqual(error.exception.reason, "invalid_output")
        self.assertEqual(self.output.read_bytes(), b"existing user file")
        self.assertFalse(self.launches)

    def test_symbolic_output_never_followed(self):
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        self.output.symlink_to(victim)
        with self.assertRaises(conversion.PdfConversionError):
            self.convert()
        self.assertEqual(victim.read_bytes(), b"keep")

    def test_symbolic_source_rejected(self):
        original = self.source
        self.source = self.root / "alias.pdf"
        self.source.symlink_to(original)
        self.assert_reason("invalid_source")

    def test_cancel_before_parse(self):
        self.assert_reason("cancelled", cancel_check=lambda: True)
        self.assertFalse(self.launches)

    def test_cancel_after_epubcheck_before_publish(self):
        self.assert_reason("cancelled", cancel_check=lambda: bool(self.validation_calls))

    def test_missing_or_failing_epubcheck_never_publishes(self):
        for code, reason in (("EPUB_VALIDATION_UNAVAILABLE", "validation_unavailable"),
                             ("EPUB_VALIDATION_FAILED", "validation_failed")):
            with self.subTest(code=code):
                self.validation = SimpleNamespace(passed=False, warnings=0, error_code=code)
                self.assert_reason(reason)

    def test_missing_memory_limit_requires_review(self):
        self.transform = lambda r: dict(r, memory_limited=False)
        result = self.convert()
        self.assertTrue(result["requires_review"])
        self.assertFalse(result["eligible_for_payment"])
        self.assertIn("memory_limit_unavailable", result["warnings"])

    def test_epubcheck_warnings_require_review(self):
        self.validation = SimpleNamespace(passed=True, warnings=1, error_code=None)
        result = self.convert()
        self.assertTrue(result["requires_review"])
        self.assertFalse(result["eligible_for_payment"])
        self.assertIn("epubcheck_warnings", result["warnings"])

    def test_parser_warning_not_automatic_payment_approval(self):
        self.transform = lambda r: dict(r, warnings=["pages_without_extractable_text"])
        self.assertFalse(self.convert()["eligible_for_payment"])

    def test_unknown_fields_never_escape_receipt(self):
        def inject(r):
            r["pages"][0]["private_manuscript"] = "NESTED_SECRET_SOURCE"
            return dict(r, private_manuscript="SECRET_SOURCE_TEXT", filename="/private/secret.pdf")
        self.transform = inject
        result = self.convert()
        self.assertNotIn("SECRET_SOURCE_TEXT", json.dumps(result))
        self.assertNotIn("NESTED_SECRET_SOURCE", json.dumps(result))
        self.assertNotIn("/private/secret.pdf", json.dumps(result))

    def test_directory_fsync_failure_rolls_back_own_publication(self):
        original_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory fsync failure")
            return original_fsync(fd)
        with patch.object(conversion.os, "fsync", fail_directory):
            self.assert_reason("invalid_output")
        self.assertFalse(any(p.name.startswith(".pdf-publish-") for p in self.root.iterdir()))

    def test_staging_cleanup_failure_rolls_back_own_publication(self):
        original_unlink = os.unlink
        injected = []
        def fail_once(path, **kwargs):
            if str(path).startswith(".pdf-publish-") and not injected:
                injected.append(True)
                raise OSError("injected staging cleanup failure")
            return original_unlink(path, **kwargs)
        with patch.object(conversion.os, "unlink", fail_once):
            self.assert_reason("invalid_output")
        self.assertTrue(injected)
        self.assertFalse(any(p.name.startswith(".pdf-publish-") for p in self.root.iterdir()))

    def test_cancel_after_publication_fsync_rolls_back(self):
        original_fsync = os.fsync
        cancelled = []
        def cancel_after_directory_sync(fd):
            original_fsync(fd)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                cancelled.append(True)
        with patch.object(conversion.os, "fsync", cancel_after_directory_sync):
            self.assert_reason("cancelled", cancel_check=lambda: bool(cancelled))
        self.assertTrue(cancelled)

    def test_worker_cleanup_failure_rolls_back_own_publication(self):
        for error in (OSError("injected worker cleanup failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                def fail_cleanup(_process):
                    self.assertTrue(self.output.is_file())
                    raise error
                with patch.object(conversion.guard, "_terminate_worker", fail_cleanup):
                    if isinstance(error, OSError):
                        self.assert_reason("invalid_output")
                    else:
                        with self.assertRaises(KeyboardInterrupt):
                            self.convert()
                self.assertFalse(self.output.exists())
                self.assertFalse(Path(self.launches[-1][1]["cwd"]).exists())

    def test_temporary_cleanup_failure_rolls_back_own_publication(self):
        original_cleanup = tempfile.TemporaryDirectory.cleanup
        for error in (OSError("injected temporary cleanup failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                def fail_cleanup(instance):
                    original_cleanup(instance)
                    if self.launches and instance.name == self.launches[-1][1]["cwd"]:
                        self.assertTrue(self.output.is_file())
                        raise error
                with patch.object(tempfile.TemporaryDirectory, "cleanup", fail_cleanup):
                    if isinstance(error, OSError):
                        self.assert_reason("invalid_output")
                    else:
                        with self.assertRaises(KeyboardInterrupt):
                            self.convert()
                self.assertFalse(self.output.exists())
                self.assertFalse(Path(self.launches[-1][1]["cwd"]).exists())

    def test_cancel_after_temporary_cleanup_rolls_back(self):
        original_cleanup = tempfile.TemporaryDirectory.cleanup
        cancelled = []
        def cancel_after_cleanup(instance):
            original_cleanup(instance)
            if self.launches and instance.name == self.launches[-1][1]["cwd"]:
                self.assertTrue(self.output.is_file())
                cancelled.append(True)
        with patch.object(tempfile.TemporaryDirectory, "cleanup", cancel_after_cleanup):
            self.assert_reason("cancelled", cancel_check=lambda: bool(cancelled))
        self.assertTrue(cancelled)

    def test_cleanup_rollback_never_removes_another_writers_inode(self):
        original_cleanup = tempfile.TemporaryDirectory.cleanup
        replacement = self.root / "replacement.epub"
        replacement.write_bytes(b"another writer owns this file")
        def replace_then_fail(instance):
            original_cleanup(instance)
            if self.launches and instance.name == self.launches[-1][1]["cwd"]:
                self.assertTrue(self.output.is_file())
                os.replace(replacement, self.output)
                raise OSError("injected cleanup failure after replacement")
        with patch.object(tempfile.TemporaryDirectory, "cleanup", replace_then_fail):
            with self.assertRaises(conversion.PdfConversionError) as error:
                self.convert()
        self.assertEqual(error.exception.reason, "invalid_output")
        self.assertEqual(self.output.read_bytes(), b"another writer owns this file")
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(any(p.name.startswith(".pdf-publish-") for p in self.root.iterdir()))

    def test_untrusted_error_not_echoed(self):
        self.transform = lambda r: {"error": "BOOK_TEXT_OR_PRIVATE_PATH"}
        self.assert_reason("worker_failed")

    def test_bad_receipt_never_publishes(self):
        mutations = [lambda r: dict(r, source_sha256="0" * 64),
                     lambda r: dict(r, output_sha256="0" * 64),
                     lambda r: dict(r, page_count=True),
                     lambda r: dict(r, pages=[]),
                     lambda r: dict(r, warnings=["Source title with private data"]),
                     lambda r: dict(r, warnings=["private_manuscript_sentinel"]),
                     lambda r: dict(r, pages=[dict(r["pages"][0], char_count=0)]),
                     lambda r: dict(r, normalized_characters=16),
                     lambda r: dict(r, memory_limited="yes")]
        for transform in mutations:
            with self.subTest(transform=transform):
                self.transform = transform
                self.assert_reason("invalid_result")

    def test_invalid_limits(self):
        for limits in ({}, replace(conversion.PdfConversionLimits(), timeout_seconds=float("nan")),
                       replace(conversion.PdfConversionLimits(), max_pages=True)):
            with self.subTest(limits=limits):
                self.assert_reason("invalid_limits", limits=limits)

    def test_source_size_limit(self):
        self.assert_reason("file_limit", limits=replace(conversion.PdfConversionLimits(), max_file_bytes=1))


if __name__ == "__main__":
    unittest.main()
