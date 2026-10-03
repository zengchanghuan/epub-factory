"""Opt-in read-only diagnostics for the user's SHA-pinned 346-page real PDF.

This is extraction-statistics coverage, NOT reading-order/semantic/translation
quality acceptance. No text is returned, no public PDF entry is enabled, and no
payment or model is invoked. The module owns only temporary parsing snapshots;
the original manuscript is never rewritten or copied into the repository.

Run separately from release_guard because the parser intentionally uses -I -B:
  EPUB_PDF_HISTORY_FILE=/absolute/user-selected.pdf python test_d56_pdf_text_history.py
The executable refuses missing opt-in inputs with exit 2 (not a skipped pass).
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.domain.pdf_text_preflight import inspect_text_pdf


SOURCE_ENV = "EPUB_PDF_HISTORY_FILE"
EXPECTED_SHA256 = "af21894c7542c1a7e3c286a762cc15c1d1aeaa81fb9ca8e028d85ff5d22b5023"
EXPECTED_PAGE_COUNT = 346
EXPECTED_SIZE = 7020179


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def original_identity(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode)


class PdfTextHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get(SOURCE_ENV, "").strip()
        if not configured:
            raise unittest.SkipTest("EPUB_PDF_HISTORY_FILE not provided; real PDF gate not executed")
        cls.path = Path(configured).absolute()
        if not cls.path.is_file():
            raise AssertionError("Configured real PDF is missing; no substitute sample is allowed")
        cls.before_identity = original_identity(cls.path)
        cls.before_sha = sha256(cls.path)
        if cls.before_sha != EXPECTED_SHA256 or cls.before_identity[2] != EXPECTED_SIZE:
            raise AssertionError("Configured real PDF does not match the user-selected SHA and size")
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.guards = [
            cls.stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError("history network forbidden")))
            for name in ("connect", "connect_ex")
        ]
        cls.guards.append(cls.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("history DNS forbidden"))))
        cls.addClassCleanup(lambda: [guard.assert_not_called() for guard in cls.guards])
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="d56-real-pdf-"))).resolve()
        cls.stack.enter_context(patch.object(tempfile, "tempdir", str(cls.root)))
        cls.stack.enter_context(patch.dict(os.environ, {
            "TMPDIR": str(cls.root), "TMP": str(cls.root), "TEMP": str(cls.root),
            "OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "", "DASHSCOPE_API_KEY": "",
            "GEMINI_API_KEY": "", "ALIPAY_APP_ID": "", "ALIPAY_PRIVATE_KEY": "",
        }))
        cls.report = inspect_text_pdf(cls.path)

    def test_every_real_page_is_reported_exactly_once_with_consistent_totals(self):
        result = self.report
        self.assertEqual(result["source_sha256"], EXPECTED_SHA256)
        self.assertEqual(result["page_count"], EXPECTED_PAGE_COUNT)
        self.assertEqual([page["page_number"] for page in result["pages"]], list(range(1, EXPECTED_PAGE_COUNT + 1)))
        self.assertGreater(result["total_nonspace_count"], 0)
        for count in ("char_count", "nonspace_count"):
            self.assertEqual(result["total_" + count], sum(page[count] for page in result["pages"]))
        self.assertEqual(result["status"], "review_required")
        self.assertIs(result["eligible_for_payment"], False)
        self.assertIn("empty_password_encryption", result["flags"])

    def test_image_only_covers_are_retained_as_pages_not_mistaken_for_missing_data(self):
        # Root visually checked these four original pages separately. This
        # contract proves classification only, not OCR or paragraph order.
        for index in (0, EXPECTED_PAGE_COUNT - 1):
            with self.subTest(page=index + 1):
                page = self.report["pages"][index]
                self.assertEqual(page["nonspace_count"], 0)
                self.assertEqual(page["kind"], "empty_or_graphic")
                self.assertGreater(page["image_xobject_count"], 0)
                self.assertIn("no_extractable_text", page["flags"])
        for index in (9, 169):
            with self.subTest(page=index + 1):
                self.assertGreater(self.report["pages"][index]["nonspace_count"], 0)
                self.assertEqual(self.report["pages"][index]["kind"], "text")

    def test_report_contains_only_approved_statistics_never_manuscript_text_or_path(self):
        result = self.report
        self.assertEqual(set(result), {"schema_version", "source_sha256", "parser_version", "status", "eligible_for_payment",
                                       "page_count", "total_char_count", "total_nonspace_count", "flags", "pages"})
        allowed_strings = {
            "pdf-text-preflight-v1", EXPECTED_SHA256, "6.0.0", "review_required", "text_candidate",
            "text", "empty_or_graphic", "parser_warnings", "memory_limit_unavailable", "empty_password_encryption",
            "no_extractable_text", "sparse_text", "replacement_characters", "control_characters", "private_use_characters",
        }
        def check_values(value):
            if isinstance(value, dict):
                for child in value.values(): check_values(child)
            elif isinstance(value, list):
                for child in value: check_values(child)
            elif isinstance(value, str):
                self.assertIn(value, allowed_strings)
            else:
                self.assertIn(type(value), (int, bool))
        check_values(result)
        page_keys = {"page_number", "kind", "status", "char_count", "nonspace_count", "replacement_count", "control_count",
                     "private_use_count", "image_xobject_count", "has_content_stream", "flags"}
        for page in result["pages"]:
            self.assertEqual(set(page), page_keys)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.path), encoded)
        self.assertNotIn(self.path.name, encoded)

    def test_original_sha_metadata_and_temporary_snapshot_cleanup_are_preserved(self):
        self.assertEqual(sha256(self.path), EXPECTED_SHA256)
        self.assertEqual(original_identity(self.path), self.before_identity)
        self.assertEqual(list(self.root.iterdir()), [])
        for guard in self.guards:
            guard.assert_not_called()


def main():
    configured = os.environ.get(SOURCE_ENV, "").strip()
    if not configured or not Path(configured).is_file():
        print("Real PDF gate requires EPUB_PDF_HISTORY_FILE pointing to the user-selected original.", file=sys.stderr)
        return 2
    program = unittest.main(verbosity=2, exit=False)
    result = program.result
    if not result.wasSuccessful() or not result.testsRun or result.skipped:
        return 1
    report = PdfTextHistoryTests.report
    print(json.dumps({
        "verification": "real_pdf_statistics_only", "source_sha256": report["source_sha256"],
        "page_count": report["page_count"], "total_char_count": report["total_char_count"],
        "total_nonspace_count": report["total_nonspace_count"], "status": report["status"],
        "eligible_for_payment": report["eligible_for_payment"], "flags": report["flags"],
        "pages_with_text": sum(page["nonspace_count"] > 0 for page in report["pages"]),
        "pages_with_images": sum(page["image_xobject_count"] > 0 for page in report["pages"]),
        "pages_requiring_review": sum(page["status"] == "review_required" for page in report["pages"]),
        "source_unchanged": True,
        "parent_network_calls": sum(guard.call_count for guard in PdfTextHistoryTests.guards),
        "child_network_policy": "deny",
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
