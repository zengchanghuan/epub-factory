"""
D16 翻译交付质检测试。

规则型质检不依赖外部模型，主要兜住：
- 有失败段落时不能交付
- 疑似未翻译段落会触发 QA 失败
- 默认不限制重译次数；显式设置上限后达到上限不再 retryable
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.domain.translation_qa_service import build_translation_qa_report


class TestTranslationQaService(unittest.TestCase):
    def test_failed_chunks_make_report_retryable(self):
        report = build_translation_qa_report(
            translation_stats={
                "total_chunks": 20,
                "failed_chunks": 3,
                "audit_flags_count": {"likely_untranslated": 2},
                "book_title_original": "The Double Helix",
                "book_title_translated": "The Double Helix",
                "free_retry_count": 0,
                "translation_attempt": 1,
            },
            output_path=None,
            error_code="PARTIAL_TRANSLATION",
        )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["retryable"])
        self.assertIn("failed_chunks", report["flags"])
        self.assertIn("likely_untranslated", report["flags"])
        self.assertIn("title_untranslated", report["flags"])
        self.assertLess(report["score"], 100)

    def test_retry_limit_disables_retryable(self):
        old_value = os.environ.get("EPUB_TRANSLATION_MAX_FREE_RETRIES")
        os.environ["EPUB_TRANSLATION_MAX_FREE_RETRIES"] = "1"
        try:
            report = build_translation_qa_report(
                translation_stats={
                    "total_chunks": 5,
                    "failed_chunks": 1,
                    "free_retry_count": 1,
                },
                error_code="PARTIAL_TRANSLATION",
            )
        finally:
            if old_value is None:
                os.environ.pop("EPUB_TRANSLATION_MAX_FREE_RETRIES", None)
            else:
                os.environ["EPUB_TRANSLATION_MAX_FREE_RETRIES"] = old_value

        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["retryable"])
        self.assertEqual(report["max_free_retries"], 1)

    def test_retry_is_unlimited_by_default(self):
        old_value = os.environ.get("EPUB_TRANSLATION_MAX_FREE_RETRIES")
        os.environ.pop("EPUB_TRANSLATION_MAX_FREE_RETRIES", None)
        try:
            report = build_translation_qa_report(
                translation_stats={
                    "total_chunks": 5,
                    "failed_chunks": 1,
                    "free_retry_count": 99,
                },
                error_code="PARTIAL_TRANSLATION",
            )
        finally:
            if old_value is not None:
                os.environ["EPUB_TRANSLATION_MAX_FREE_RETRIES"] = old_value

        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["retryable"])
        self.assertEqual(report["max_free_retries"], -1)


if __name__ == "__main__":
    unittest.main()
