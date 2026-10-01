"""R4 opt-in history: real EPUB identity replay and retained Markdown uploads.

Inherits the D40 three-book conversion/reduce/EPUBCheck safety gate. Markdown
uploads are historical smoke samples, not customer-length books; no historical
DOCX is available. Only local parsing, pricing and non-AI preprocessing run.
This is not an online translation-quality or payment acceptance test.
"""
from __future__ import annotations

import io
import json
import os
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest.mock import patch

import test_d40_reduce_history as replay


navigation = replay.tables.navigation
MARKDOWN_UPLOADS = (
    ("648a9ae99861-smoke2.md", "0dcca1bc464b4d2830665eb5c1baad944d4a64e133ed0f1705226c7d10d763fd"),
    ("e50f80aa5f37-smoke_test.md", "c04e3d3db09105518c504bac9d00da98aa99a813409e6dd484e1b7db67027a68"),
    ("46a7794559df-smoke_test.md", "c04e3d3db09105518c504bac9d00da98aa99a813409e6dd484e1b7db67027a68"),
)


class TranslationInputHistoryTests(replay.ReduceHistoryTests):
    @classmethod
    def setUpClass(cls):
        environment = patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "", "DASHSCOPE_API_KEY": "", "GEMINI_API_KEY": "",
            "EPUB_FAST_TRANSLATION": "1", "SMTP_HOST": "",
        })
        environment.start()
        cls.addClassCleanup(environment.stop)
        super().setUpClass()
        from bs4 import BeautifulSoup
        from app.domain.translation_input import normalized_translation_input
        from app.domain.manifest_service import build_manifest
        from app.engine.adapters.markdown_adapter import md_to_html
        from app.engine.compiler import EPUBCHECK_JAR, ExtremeCompiler
        from app.engine.epub_validation import validate_epub
        from app.main import _prepare_translation_request
        from app.engine import translation_cache

        real_cache = translation_cache.TranslationCache
        cls.stack.enter_context(patch.object(
            translation_cache, "TranslationCache",
            side_effect=lambda *_args, **_kwargs: real_cache(str(cls.root / "quote-cache.sqlite3")),
        ))

        cls.normalize = staticmethod(normalized_translation_input)
        cls.markdown_records = []
        for index, (name, expected_hash) in enumerate(MARKDOWN_UPLOADS):
            source = cls.uploads / name
            if navigation.sha256(source) != expected_hash:
                raise AssertionError(f"Historical Markdown fixture changed: {name}")
            rendered, _metadata = md_to_html(source)
            source_body = BeautifulSoup(rendered, "html.parser")
            expected_text = navigation.normalized_text(source_body.get_text("", strip=False), cls.opencc)
            expected_blocks = [node.name for node in source_body.find_all(["h1", "h2", "h3", "p", "li"])]
            with normalized_translation_input(source, source_name=name) as first:
                first_path = first.epub_path
                first_hash = navigation.sha256(first_path)
                normalized_validation = validate_epub(first_path, EPUBCHECK_JAR)
                with zipfile.ZipFile(first_path) as archive:
                    body = BeautifulSoup(archive.read("OEBPS/chapter1.xhtml"), "html.parser").body
                    actual_text = navigation.normalized_text(body.get_text("", strip=False), cls.opencc)
                    actual_blocks = [node.name for node in body.find_all(["h1", "h2", "h3", "p", "li"])]
                prepared = cls.root / f"markdown-{index}-prepared.epub"
                with redirect_stdout(io.StringIO()):
                    manifest = build_manifest(str(first_path), f"d42-history-md-{index}")
                    compiler = ExtremeCompiler(str(first_path), str(prepared), output_mode="simplified",
                                               enable_translation=False, lexicon_domains=[], enable_proper_noun=False)
                    generated = compiler.run()
                if not generated or not prepared.is_file():
                    raise AssertionError(f"Historical Markdown preprocessing failed: {index}")
                prepared_snapshot = navigation.BookSnapshot(prepared, cls.opencc)
                original_snapshot = navigation.BookSnapshot(first_path, cls.opencc)
                identity = (first.source_sha256, first.adapter, first.normalization_version)
            with normalized_translation_input(source, source_name=name) as second:
                second_path = second.epub_path
                second_hash = navigation.sha256(second_path)
                with redirect_stdout(io.StringIO()):
                    second_manifest = build_manifest(str(second_path), f"d42-history-md-{index}")
            pricing, preflight, quote_identity, warnings = _prepare_translation_request(
                input_path=source, source_name=name, job_id=f"d42-history-md-{index}",
                target_lang="zh", translation_model="deepseek-flash", translation_quality="high",
                translation_strategy="auto", glossary=None, profile_confirmation=False,
            )
            cls.markdown_records.append({
                "index": index, "source": source, "source_hash": expected_hash,
                "expected_text": expected_text, "actual_text": actual_text,
                "expected_blocks": expected_blocks, "actual_blocks": actual_blocks,
                "first_path": first_path, "second_path": second_path,
                "first_hash": first_hash, "second_hash": second_hash, "identity": identity,
                "normalized_validation": normalized_validation, "compiler": compiler,
                "original_snapshot": original_snapshot, "prepared_snapshot": prepared_snapshot,
                "manifest": manifest, "second_manifest": second_manifest, "pricing": pricing,
                "preflight": preflight, "quote_identity": quote_identity, "warnings": warnings,
            })
        # Counts and hashes only: never emit historical book/sample prose.
        print("R4 historical Markdown evidence: " + json.dumps([{
            "sample": row["index"], "source_sha256": row["source_hash"],
            "blocks": len(row["actual_blocks"]), "quoted_chars": row["pricing"].get("total_chars"),
            "chapters": len(row["manifest"].get("chapters", [])),
        } for row in cls.markdown_records], sort_keys=True))

    @classmethod
    def tearDownClass(cls):
        for name, expected_hash in MARKDOWN_UPLOADS:
            if navigation.sha256(cls.uploads / name) != expected_hash:
                raise AssertionError(f"Read-only historical Markdown changed: {name}")
        super().tearDownClass()

    def test_epub_normalization_is_read_only_passthrough_for_three_actual_books(self):
        for book in navigation.BOOKS:
            source = self.uploads / book["input"]
            with self.subTest(book=book["key"]), self.normalize(source) as normalized:
                self.assertEqual(normalized.epub_path, source)
                self.assertEqual(normalized.source_sha256, book["input_sha256"])
                self.assertEqual(normalized.adapter, "epub")
            self.assertTrue(source.is_file(), "Passthrough must never clean up an original")

    def test_markdown_text_and_block_order_survive_normalization_and_preprocessing(self):
        for row in self.markdown_records:
            with self.subTest(sample=row["index"]):
                self.assertTrue(row["expected_text"] == row["actual_text"], "Normalization changed historical text")
                self.assertEqual(row["expected_blocks"], row["actual_blocks"])
                before, after = row["original_snapshot"], row["prepared_snapshot"]
                for name in before.docs.keys() - before.nav_docs:
                    self.assertIn(name, after.docs)
                    self.assertTrue(before.docs[name]["text"] == after.docs[name]["text"],
                                    "Non-AI preprocessing changed historical text")

    def test_markdown_normalization_and_manifest_are_repeatable_and_temporary(self):
        for row in self.markdown_records:
            with self.subTest(sample=row["index"]):
                self.assertEqual(row["first_hash"], row["second_hash"])
                self.assertFalse(row["first_path"].exists())
                self.assertFalse(row["second_path"].exists())
                self.assertEqual(row["manifest"], row["second_manifest"])
                self.assertFalse(row["manifest"].get("error"))
                self.assertTrue(row["manifest"].get("chapters"))
                self.assertEqual(row["identity"], (row["source_hash"], "markdown", "translation-input-v1"))

    def test_markdown_quote_is_nonzero_and_tied_to_original_hash(self):
        for row in self.markdown_records:
            with self.subTest(sample=row["index"]):
                self.assertGreater(row["pricing"].get("total_chars", 0), 0)
                self.assertEqual(row["quote_identity"].get("source_sha256"), row["source_hash"])
                self.assertEqual(row["quote_identity"].get("adapter"), "markdown")
                self.assertIsNone(row["preflight"])

    def test_markdown_normalized_and_preprocessed_artifacts_pass_real_epubcheck(self):
        for row in self.markdown_records:
            with self.subTest(sample=row["index"]):
                self.assertTrue(row["normalized_validation"].passed, row["normalized_validation"].message)
                self.assertEqual(row["compiler"].metrics.mode, "full")
                self.assertTrue(row["compiler"].validation_passed, row["compiler"].final_message)
                report = self.validation_reports[f"markdown-{row['index']}-prepared.epub"]
                self.assertEqual(report["checker"]["nError"], 0)
                self.assertEqual(report["checker"]["nFatal"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
