"""Regression for paid orders blocked by an already-Chinese book title."""
import tempfile
import unittest
from pathlib import Path

from app.domain.translation_qa_service import build_translation_qa_report


class ChineseTitleQaTests(unittest.TestCase):
    def report(self, title, target="zh-CN", **extra):
        stats = {
            "book_title_original": title,
            "book_title_translated": title,
            "artifact_audit": {"status": "passed", "target_lang": target},
            "total_chunks": 5844,
            "failed_chunks": 0,
        }
        stats.update(extra)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.epub"
            output.write_bytes(b"existing output; EPUB validation is a separate gate")
            return build_translation_qa_report(translation_stats=stats, output_path=output)

    def test_existing_chinese_title_can_be_delivered(self):
        for title, target in [("英语精读合集（三册）", "zh-CN"),
                              ("英語精讀合集（第3冊）", "zh-TW")]:
            with self.subTest(title=title, target=target):
                report = self.report(title, target)
                self.assertTrue(report["can_deliver"])
                self.assertNotIn("title_untranslated", report["flags"])

    def test_unchanged_foreign_and_mixed_titles_still_fail(self):
        for title in ["The Double Helix", "Dune", "English 精读合集", "日本語の本",
                      "中文 Русский", "中文 한국어", "中文 Ｅｎｇｌｉｓｈ", "2026"]:
            with self.subTest(title=title):
                report = self.report(title)
                self.assertIn("title_untranslated", report["flags"])
                self.assertFalse(report["can_deliver"])

    def test_requires_explicit_chinese_target(self):
        for target in ["en", "ja", "", None]:
            with self.subTest(target=target):
                report = self.report("英语精读合集（三册）", target)
                self.assertIn("title_untranslated", report["flags"])
                self.assertFalse(report["can_deliver"])

    def test_chinese_title_does_not_bypass_other_failures(self):
        for extra, flag in [
            ({"failed_chunks": 1}, "failed_chunks"),
            ({"artifact_audit": {"status": "passed", "target_lang": "zh-CN", "residual_blocks": 1}},
             "artifact_untranslated_blocks"),
            ({"artifact_audit": {"status": "passed", "target_lang": "zh-CN", "navigation_residual_labels": 1}},
             "navigation_untranslated"),
        ]:
            with self.subTest(flag=flag):
                report = self.report("英语精读合集（三册）", **extra)
                self.assertIn(flag, report["flags"])
                self.assertFalse(report["can_deliver"])

    def test_keeps_manual_review_warning(self):
        report = self.report("英语精读合集（三册）", audit_warn_chunks=160)
        self.assertTrue(report["can_deliver"])
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["review_chunks"], 160)


if __name__ == "__main__":
    unittest.main()
