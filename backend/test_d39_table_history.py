"""B2: real historical EPUBs, strict EPUBCheck and independent table protection.

Opt in with EPUB_HISTORY_UPLOAD_DIR and EPUB_HISTORY_OUTPUT_DIR. No source book
is replaced with a synthetic EPUB. Inherits B1's isolated non-AI conversion,
network guards, source/output SHA pins and content/navigation assertions.
Unlike B1, all three new artifacts must have zero EPUBCheck ERROR/FATAL.
"""
from __future__ import annotations

import hashlib
import re
import unittest
import zipfile
from collections import Counter

import test_d38_navigation_history as navigation


TABLE_DOCUMENT = "text/part0110.html"
TABLE_TEXT_SHA256 = "abb94af0456d84a5800031b0d8797a37653c8ecb3c0e4fc98824b649f5a35b37"
TABLE_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "col"}


def declarations(style):
    """Independent parser; preserve values and !important, never call repair code."""
    import cssutils
    parsed = cssutils.CSSParser(validate=False, fetcher=lambda _url: (None, None)).parseStyle(style or "")
    return {prop.name: (prop.value, prop.priority) for prop in parsed}


def tables_from_zip(path, document_names):
    from bs4 import BeautifulSoup
    result = {}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for name in document_names:
            candidates = [member for member in names if member == name or member.endswith("/" + name)]
            if len(candidates) != 1:
                raise AssertionError(f"Ambiguous historical document member: {name}")
            soup = BeautifulSoup(archive.read(candidates[0]).decode("utf-8"), "html.parser")
            tables = soup.find_all("table")
            if tables:
                result[name] = tables
    return result


def class_declarations_from_zip(path, classes):
    """Read existing author rules directly; CSS file naming may change on package."""
    import cssutils
    parser = cssutils.CSSParser(validate=False, fetcher=lambda _url: (None, None))
    result = Counter()
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".css"):
                continue
            for rule in parser.parseString(archive.read(name)):
                if rule.type != rule.STYLE_RULE:
                    continue
                for selector in rule.selectorText.split(","):
                    if any(re.search(r"\." + re.escape(token) + r"(?![\w-])", selector) for token in classes):
                        for prop in rule.style:
                            result[(selector.strip(), prop.name, prop.value, prop.priority)] += 1
    return result


class TableHistoryTests(navigation.NavigationHistoryTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.table_runs = {}
        for key, (before, after, _) in cls.runs.items():
            original = cls.root / (key + "-source.epub")
            converted = cls.root / (key + "-converted.epub")
            source = tables_from_zip(original, before.docs)
            target = tables_from_zip(converted, after.docs)
            classes = {token for tables in source.values() for table in tables
                       for node in [table, *table.find_all(True)]
                       for token in node.get("class", [])}
            cls.table_runs[key] = (source, target,
                                  class_declarations_from_zip(original, classes),
                                  class_declarations_from_zip(converted, classes))

    def test_actual_epubcheck_fixes_target_without_new_errors_elsewhere(self):
        # Override the B1 allowance: the previously known Double Helix errors
        # are now precisely the defect under test, never accepted as baseline.
        for key, (_, _, compiler) in self.runs.items():
            with self.subTest(book=key):
                self.assertEqual(compiler.metrics.mode, "full")
                self.assertTrue(compiler.validation_passed, compiler.final_message)
                self.assertIsNotNone(compiler._epubcheck_result)
                self.assertIn(key + "-converted.epub", self.validation_reports)
                report = self.validation_reports[key + "-converted.epub"]
                self.assertEqual(report["checker"]["nError"], 0)
                self.assertEqual(report["checker"]["nFatal"], 0)
                self.assertFalse([message for message in report["messages"]
                                  if message["severity"] in {"ERROR", "FATAL"}])

    def test_all_table_cells_columns_attributes_and_author_css_are_preserved(self):
        exercised = 0
        for key, (source, target, source_css, target_css) in self.table_runs.items():
            self.assertEqual(set(source), set(target), f"Table documents changed: {key}")
            self.assertFalse(source_css - target_css, f"Original table author CSS declarations disappeared: {key}")
            for name, original_tables in source.items():
                self.assertEqual(len(original_tables), len(target[name]), f"Table count changed: {key} {name}")
                for index, (original, converted) in enumerate(zip(original_tables, target[name])):
                    with self.subTest(book=key, document=name, table=index):
                        def grid(table):
                            return [[(cell.name, cell.get("rowspan", "1"), cell.get("colspan", "1"),
                                      navigation.normalized_text(cell.get_text(), self.opencc))
                                     for cell in row.find_all(["td", "th"], recursive=False)]
                                    for row in table.find_all("tr") if row.find_parent("table") is table]
                        self.assertEqual(grid(original), grid(converted), "Cell row/column order, spans or text changed")
                        old_nodes = [original, *original.find_all(TABLE_TAGS)]
                        new_nodes = [converted, *converted.find_all(TABLE_TAGS)]
                        self.assertEqual([node.name for node in old_nodes], [node.name for node in new_nodes],
                                         "Original table structure changed beyond colgroup compatibility wrapping")
                        for old, new in zip(old_nodes, new_nodes):
                            for attribute in ("id", "class", "headers", "scope", "abbr", "span", "rowspan", "colspan"):
                                if old.has_attr(attribute):
                                    self.assertEqual(old[attribute], new.get(attribute), f"Table semantic {attribute} changed")
                            for prop, value in declarations(old.get("style")).items():
                                self.assertEqual(declarations(new.get("style")).get(prop), value,
                                                 f"Existing inline CSS changed: {old.name} {prop}")
                        old_columns, new_columns = original.find_all("col"), converted.find_all("col")
                        self.assertEqual(len(old_columns), len(new_columns))
                        for old, new in zip(old_columns, new_columns):
                            if old.has_attr("width"):
                                value = old["width"]
                                expected = value + "px" if re.fullmatch(r"\d+(?:\.\d+)?", value) else value
                                self.assertEqual(declarations(new.get("style")).get("width", (None,))[0], expected)
                            self.assertEqual(new.parent.name, "colgroup", "Direct table col remains invalid XHTML")
                        exercised += 1
        self.assertGreater(exercised, 0, "No real historical table exercised")

    def test_double_helix_original_table_identity_and_css_precedence(self):
        before, after, _ = self.runs["double-helix"]
        source, target, source_css, target_css = self.table_runs["double-helix"]
        self.assertEqual(sum(before.images.values()), 308)
        self.assertEqual(before.images, after.images)
        self.assertEqual(len(source[TABLE_DOCUMENT]), 1)
        original, converted = source[TABLE_DOCUMENT][0], target[TABLE_DOCUMENT][0]
        self.assertEqual(hashlib.sha256(original.get_text().encode("utf-8")).hexdigest(), TABLE_TEXT_SHA256)
        self.assertEqual(len(original.find_all(["td", "th"])), 14)
        self.assertEqual([len(row.find_all(["td", "th"], recursive=False)) for row in original.find_all("tr")], [2] * 7)
        self.assertEqual([col.get("width") for col in original.find_all("col")], ["20%", "80%"])
        self.assertEqual([declarations(col.get("style"))["width"][0] for col in converted.find_all("col")], ["20%", "80%"])

        # These are deliberately conflicting source hints. HTML presentation
        # hints lose to existing author CSS; moving hints to inline styles must
        # not silently promote their cascade priority.
        for declaration in [
            (".calibre175", "border-spacing", "2px", ""),
            (".calibre175", "border-collapse", "separate", ""),
            (".calibre177", "vertical-align", "middle", ""),
            (".calibre178", "vertical-align", "inherit", ""),
            (".calibre178", "padding", "1px", ""),
        ]:
            self.assertIn(declaration, source_css, "Pinned source CSS evidence missing")
            self.assertIn(declaration, target_css, "Existing author table CSS lost")
        table_style = declarations(converted.get("style"))
        for prop, value in (("border-spacing", "2px"), ("border-collapse", "separate")):
            if prop in table_style:
                self.assertEqual(table_style[prop][0], value, "Legacy table hint overrides author CSS")
        for cell in converted.find_all(["td", "th"]):
            style = declarations(cell.get("style"))
            for prop in ("padding", "padding-top", "padding-right", "padding-bottom", "padding-left"):
                if prop in style:
                    self.assertEqual(style[prop][0], "1px", "Legacy cellpadding overrides author padding")
            if "vertical-align" in style:
                self.assertEqual(style["vertical-align"][0], "inherit", "Legacy valign overrides author inheritance")
        for attribute in ("cellspacing", "cellpadding"):
            self.assertEqual(original[attribute], "0")
            self.assertNotIn(attribute, converted.attrs)
            self.assertEqual(converted.get("data-legacy-" + attribute), original[attribute],
                             "Conflicting source presentation hint should remain traceable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
