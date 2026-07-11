"""
D16 翻译交付质检测试。

规则型质检不依赖外部模型，主要兜住：
- 有失败段落时不能交付
- 疑似未翻译段落会触发 QA 失败
- 默认不限制重译次数；显式设置上限后达到上限不再 retryable
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from app.domain.translation_qa_service import audit_translated_epub_output, build_translation_qa_report
from app.domain.image_caption_repair import repair_image_captions


class TestTranslationQaService(unittest.TestCase):
    def test_caption_repair_translates_text_and_preserves_image_media(self):
        class FakeStats:
            @staticmethod
            def to_dict():
                return {"translated_chunks": 1}

        class FakeTranslator:
            def __init__(self, *args, **kwargs):
                self.stats = FakeStats()

            async def translate_many_chunks_async(self, chunks, progress_label=None):
                self.assert_caption(chunks)
                return [
                    SimpleNamespace(
                        translated_html='<span class="txbdit">威廉·西兹在物理系聚会上。</span>',
                        error=None,
                    )
                ]

            @staticmethod
            def assert_caption(chunks):
                assert len(chunks) == 1
                assert "William Seeds" in chunks[0]

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.epub"
            output = Path(tmp) / "output.epub"
            with zipfile.ZipFile(source, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                zf.writestr(
                    "EPUB/text/chapter1.html",
                    """
                    <html><body>
                    <p class="imagegr"><img src="../images/photo.jpg"/></p>
                    <p class="caption"><span class="txbdit">William Seeds stands at a Physics Department party in the early 1950s.</span></p>
                    <p>这一段已经翻译完成。</p>
                    </body></html>
                    """,
                )
                zf.writestr("EPUB/images/photo.jpg", b"fake-image")

            with patch("app.domain.image_caption_repair.SemanticsTranslator", FakeTranslator):
                report = asyncio.run(repair_image_captions(source, output))

            with zipfile.ZipFile(output) as zf:
                self.assertEqual(zf.infolist()[0].filename, "mimetype")
                self.assertEqual(zf.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
                self.assertEqual(zf.read("EPUB/images/photo.jpg"), b"fake-image")
                html = zf.read("EPUB/text/chapter1.html").decode("utf-8")

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["caption_blocks"], 1)
        self.assertIn("威廉·西兹", html)
        self.assertNotIn("William Seeds stands", html)

    def test_caption_repair_accepts_local_map_and_preserves_page_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.epub"
            output = Path(tmp) / "output.epub"
            translations = Path(tmp) / "captions.json"
            caption = "William Seeds stands at a Physics Department party."
            translations.write_text(
                json.dumps(
                    [
                        {
                            "source_text": caption,
                            "translated_text": "威廉·西兹站在物理系的一场聚会上。",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with zipfile.ZipFile(source, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                zf.writestr(
                    "EPUB/text/chapter1.html",
                    f'<html><body><p class="caption"><span>{caption}</span><a id="page_94"></a></p></body></html>',
                )

            report = asyncio.run(
                repair_image_captions(source, output, translations_json=translations)
            )

            with zipfile.ZipFile(output) as zf:
                html = zf.read("EPUB/text/chapter1.html").decode("utf-8")

        self.assertEqual(report["translation_stats"]["mode"], "local_translation_map")
        self.assertIn("威廉·西兹站在物理系的一场聚会上。", html)
        self.assertIn('id="page_94"', html)
        self.assertNotIn(caption, html)

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

    def test_artifact_audit_fails_on_untranslated_body_paragraph(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / "translated.epub"
            with zipfile.ZipFile(epub_path, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip")
                zf.writestr(
                    "EPUB/text/chapter1.html",
                    """
                    <html><body>
                    <h2>第1章</h2>
                    <p>This chapter still contains a long English paragraph that should have been translated before delivery.</p>
                    <p>这一段已经翻译完成。</p>
                    </body></html>
                    """,
                )

            audit = audit_translated_epub_output(epub_path, target_lang="zh-CN")

        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["residual_blocks"], 1)
        self.assertEqual(audit["residual_categories"]["long_english_no_cjk"], 1)
        self.assertEqual(audit["samples"][0]["file"], "EPUB/text/chapter1.html")

    def test_artifact_audit_ignores_non_body_but_checks_caption_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / "translated.epub"
            with zipfile.ZipFile(epub_path, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip")
                zf.writestr(
                    "EPUB/text/sources.html",
                    """
                    <html><body>
                    <h2>Sources</h2>
                    <p>This bibliography style paragraph is intentionally left in English for citation context.</p>
                    </body></html>
                    """,
                )
                zf.writestr(
                    "EPUB/text/chapter1.html",
                    """
                    <html><body>
                    <h2>第1章</h2>
                    <p class="caption">William Seeds stands at a Physics Department party in the early 1950s while Ray Gosling is partly hidden behind him.</p>
                    <p class="endnotes"><sup>2</sup> Waismann, Erkenntnis 1, 1930, p. 229.</p>
                    <p>这一段已经翻译完成。</p>
                    </body></html>
                    """,
                )

            audit = audit_translated_epub_output(epub_path, target_lang="zh-CN")

        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["residual_blocks"], 1)
        self.assertEqual(audit["samples"][0]["class"], "caption")

    def test_artifact_audit_checks_explanatory_footnote_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / "translated.epub"
            with zipfile.ZipFile(epub_path, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip")
                zf.writestr(
                    "EPUB/text/chapter1.html",
                    """
                    <html><body>
                    <h2>第1章</h2>
                    <p class="footnote"><sup>3</sup>This explanatory note contains a complete English argument that readers need in order to understand the chapter.</p>
                    </body></html>
                    """,
                )

            audit = audit_translated_epub_output(epub_path, target_lang="zh-CN")

        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["residual_blocks"], 1)

    def test_artifact_audit_scan_error_blocks_qa(self):
        audit = audit_translated_epub_output("/tmp/nonexistent-qa-output.epub", target_lang="zh-CN")
        report = build_translation_qa_report(
            translation_stats={"artifact_audit": audit},
            error_code="PARTIAL_TRANSLATION",
        )

        self.assertEqual(audit["status"], "scan_error")
        self.assertEqual(report["status"], "failed")
        self.assertIn("artifact_scan_error", report["flags"])

    def test_report_includes_artifact_untranslated_flag(self):
        report = build_translation_qa_report(
            translation_stats={
                "total_chunks": 20,
                "failed_chunks": 0,
                "artifact_audit": {
                    "status": "failed",
                    "residual_blocks": 2,
                    "checked_text_blocks": 20,
                },
            },
            error_code="PARTIAL_TRANSLATION",
        )

        self.assertEqual(report["status"], "failed")
        self.assertIn("artifact_untranslated_blocks", report["flags"])


if __name__ == "__main__":
    unittest.main()
