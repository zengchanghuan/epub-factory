"""Offline deterministic quote tests; no app credentials, cache or model use."""
from __future__ import annotations

import socket
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from app.domain.translation_quote import QuoteInputError, estimate_quote


class TranslationQuoteTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="d52-quote-")))
        for target in ("connect", "connect_ex", "sendto"):
            self.stack.enter_context(patch.object(socket.socket, target, side_effect=AssertionError("network forbidden")))
        self.cache = self.stack.enter_context(patch(
            "app.engine.translation_cache.TranslationCache", side_effect=AssertionError("quotes must not open the runtime cache")))
        self.price = Mock(return_value="12.34")

    def epub(self, pages=None):
        source = self.root / "book.epub"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            for name, content in (pages or {"chapter.xhtml": "<html><body><p>Hello world.</p></body></html>"}).items():
                archive.writestr(name, content)
        return source

    def quote(self, source=None, **kwargs):
        return estimate_quote(source or self.epub(), "zh-CN", {"World": "世界"}, price_for_chars=self.price, **kwargs)

    def test_fresh_never_claims_a_cache_hit_or_discount(self):
        quote = self.quote(cache_policy="fresh")
        self.assertEqual(quote["schema_version"], 2)
        self.assertEqual(quote["quote_type"], "service_quote")
        self.assertEqual(quote["cache_policy"], "fresh")
        self.assertEqual(quote["cache_discount_status"], "disabled")
        self.assertEqual(quote["total_chars"], 12)
        self.assertEqual(quote["billable_chars"], 12)
        self.assertEqual(quote["cached_chars"], 0)
        self.assertIsNone(quote["hit_ratio"])
        self.assertEqual(quote["price_cny"], "12.34")
        self.assertEqual(quote["raw_price_cny"], quote["price_cny"])
        self.cache.assert_not_called()

    def test_reuse_and_verified_explicitly_defer_discount_not_runtime_reuse(self):
        for policy in ("reuse", "verified"):
            with self.subTest(policy=policy):
                quote = self.quote(cache_policy=policy)
                self.assertEqual(quote["cache_policy"], policy)
                self.assertEqual(quote["cache_discount_status"], "deferred")
                self.assertEqual(quote["cached_chars"], 0)
                self.assertIsNone(quote["hit_ratio"])
                self.assertEqual(quote["billable_chars"], quote["total_chars"])
                self.assertEqual(quote["price_cny"], quote["raw_price_cny"])
        self.cache.assert_not_called()

    def test_invalid_policy_rejected_before_reading_source_or_pricing(self):
        for policy in ("", "FRESH", "fresh ", "unknown", None, 1, True, []):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "cache_policy"):
                self.quote(self.root / "missing.epub", cache_policy=policy)
        self.price.assert_not_called()
        self.cache.assert_not_called()

    def test_leaf_inner_html_basis_preserves_inline_markup_and_no_parent_double_count(self):
        source = self.epub({"chapter.xhtml": "<html><body><div>parent<p> A <b>B</b> </p><p>C</p></div><h1>Title</h1><div>only</div></body></html>"})
        quote = self.quote(source)
        self.assertEqual(quote["total_chars"], len("A <b>B</b>") + len("C") + len("Title") + len("only"))

    def test_all_supported_page_suffixes_scanned_and_other_resources_ignored(self):
        pages = {name: "<html><body><p>ok</p></body></html>" for name in ("a.xhtml", "b.HTML", "c.htm")}
        pages.update({"notes.txt": "ignored", "bad.xml": b"\xff"})
        self.assertEqual(self.quote(self.epub(pages))["total_chars"], 6)

    def test_callback_receives_character_count_exactly_once(self):
        self.quote(translation_quality="literary", translation_model="deepseek-v4-pro")
        self.price.assert_called_once_with(12)

    def test_operator_fixed_quote_is_not_reinterpreted_as_a_discount_or_floor(self):
        self.price.return_value = "0.01"
        quote = self.quote()
        self.assertEqual((quote["price_cny"], quote["raw_price_cny"]), ("0.01", "0.01"))
        self.price.assert_called_once()

    def test_amount_callback_failure_propagates_without_fabricated_quote(self):
        self.price.side_effect = ValueError("invalid pricing configuration")
        with self.assertRaisesRegex(ValueError, "invalid pricing configuration") as error:
            self.quote()
        self.assertNotIsInstance(error.exception, QuoteInputError)

    def test_unreadable_or_non_zip_source_rejected_without_floor(self):
        invalid = self.root / "invalid.epub"
        invalid.write_bytes(b"not a zip")
        for source in (self.root / "missing.epub", invalid, self.root):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "无法读取 EPUB"):
                self.quote(source)
        self.price.assert_not_called()

    def test_archive_without_content_pages_is_not_a_minimum_price_order(self):
        with self.assertRaisesRegex(ValueError, "未包含可读取"):
            self.quote(self.epub({"style.css": "body {}"}))
        self.price.assert_not_called()

    def test_valid_empty_page_counts_zero_and_does_not_hide_good_page(self):
        source = self.epub({"empty.xhtml": "<html><head><title>Empty</title></head><body></body></html>",
                            "good.xhtml": "<html><body><p>Good</p></body></html>"})
        self.assertEqual(self.quote(source)["total_chars"], 4)
        self.assertEqual(self.quote(self.epub({"empty.xhtml": "<html><body></body></html>"}))["total_chars"], 0)

    def test_any_bad_page_rejects_quote_instead_of_partial_underpricing(self):
        from app.domain import translation_quote
        parse = translation_quote.BeautifulSoup
        source = self.epub({"good.xhtml": "<p>Good</p>", "bad.xhtml": "unreadable fixture"})
        for failure in (RuntimeError("parser failed"), Mock(contains_replacement_characters=True)):
            def parser(raw, kind):
                if raw in (b"unreadable fixture", "unreadable fixture"):
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                return parse(raw, kind)
            with self.subTest(failure=failure), patch.object(translation_quote, "BeautifulSoup", side_effect=parser):
                with self.assertRaisesRegex(ValueError, "bad.xhtml"):
                    self.quote(source)
        self.price.assert_not_called()

    def test_real_corrupted_zip_page_rejects_quote_after_readable_page(self):
        source = self.epub({"good.xhtml": "<p>Good</p>", "bad.xhtml": "<p>corrupt_me</p>"})
        # Stored ZIP data keeps a CRC: modifying only its payload produces a
        # genuine read failure, not a mocked parser or an invalid whole archive.
        raw = source.read_bytes()
        self.assertEqual(raw.count(b"corrupt_me"), 1)
        source.write_bytes(raw.replace(b"corrupt_me", b"corrupt_NO"))
        with self.assertRaisesRegex(QuoteInputError, "bad.xhtml"):
            self.quote(source)
        self.price.assert_not_called()

    def test_fragment_and_declared_non_utf8_pages_keep_existing_compatibility(self):
        for page in (b"<p>caf\xe9</p>", b'<?xml version="1.0" encoding="iso-8859-1"?><html><body><p>caf\xe9</p></body></html>'):
            with self.subTest(page=page):
                self.assertEqual(self.quote(self.epub({"a.xhtml": page}))["total_chars"], 4)

    def test_utf8_and_utf16_bom_pages_count_without_replacement_characters(self):
        text = "<html><body><p>读书</p></body></html>"
        for encoding in ("utf-8-sig", "utf-16"):
            with self.subTest(encoding=encoding):
                self.assertEqual(self.quote(self.epub({"a.xhtml": text.encode(encoding)}))["total_chars"], 2)

    def test_short_utf8_without_bom_preserves_prior_character_count(self):
        from bs4 import BeautifulSoup
        for word in ("读书", "责任", "世界", "司法独立"):
            raw = ("<p>" + word + "</p>").encode("utf-8")
            old_soup = BeautifulSoup(raw.decode("utf-8"), "html.parser")
            old_chars = len("".join(str(child) for child in old_soup.p.contents).strip())
            with self.subTest(word=word):
                self.assertEqual(self.quote(self.epub({"a.xhtml": raw}))["total_chars"], old_chars)

    def test_read_failure_is_an_input_error_not_a_pricing_configuration_error(self):
        with self.assertRaises(QuoteInputError):
            self.quote(self.root / "missing.epub")
        self.price.assert_not_called()

    def test_legacy_cache_file_cannot_change_quote_and_is_not_touched(self):
        cache_path = self.root / "translation_cache.db"
        cache_path.write_bytes(b"not even a sqlite database")
        original = cache_path.read_bytes()
        first = self.quote()
        cache_path.write_bytes(b"different stale runtime cache")
        second = self.quote()
        self.assertEqual(first, second)
        self.assertEqual(cache_path.read_bytes(), b"different stale runtime cache")
        self.assertNotEqual(original, cache_path.read_bytes())
        self.cache.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
