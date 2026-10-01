"""B1 historical navigation regression with independent content-preservation checks.

Opt in using EPUB_HISTORY_UPLOAD_DIR and EPUB_HISTORY_OUTPUT_DIR, as for D37.
EPUB_HISTORY_BASELINE_DIR additionally enables the EPUBCheck non-regression gate
against pre-B1 converted artifacts (<input SHA-256 prefix>/converted.epub).
All three actual historical books are converted locally without AI. Original
uploads and existing delivered artifacts remain read-only and SHA-256 pinned.
The checks parse the EPUB directly rather than reusing navigation repair code.
"""
from __future__ import annotations

import io
import json
import copy
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_d37_entitlement_history import BOOKS, sha256


def normalized_text(text, opencc):
    """Only documented OpenCC/typographic normalization, never drop prose."""
    text = opencc.convert(unicodedata.normalize("NFKC", text))
    text = re.sub(r"\.{3,}", "…", text)
    text = re.sub(r"(?<![:/\-])\-{2}(?![\-/>])", "—", text)
    return re.sub(r"\s+", "", text)


def heading_evidence_label(node, soup, archive, physical_name, opencc):
    """Read original local CSS independently to identify an actual note marker."""
    children = [child for child in node.children if getattr(child, "name", None)]
    if not children:
        return normalized_text(node.get_text("", strip=False), opencc)
    marker = children[-1]
    numeric = re.fullmatch(r"[0-9]+", marker.get_text("", strip=False).strip())
    trailing = "".join(str(sibling) for sibling in marker.next_siblings).strip()
    is_super = marker.name == "sup"
    inline = re.search(r"(?:^|;)\s*vertical-align\s*:\s*([^;]+)", marker.get("style") or "", re.I)
    if inline:
        is_super = inline.group(1).strip().lower() == "super"
    elif not is_super and numeric:
        import cssutils
        parser = cssutils.CSSParser(validate=False, fetcher=lambda _url: (None, None))
        styles = [style.get_text("", strip=False) for style in soup.find_all("style")]
        for link in soup.find_all("link", href=True):
            if "stylesheet" not in (link.get("rel") or []):
                continue
            uri = urlsplit(link["href"])
            if uri.scheme or uri.netloc:
                continue
            name = posixpath.normpath(posixpath.join(posixpath.dirname(physical_name), unquote(uri.path)))
            if name in archive.namelist():
                styles.append(archive.read(name))
        classes = set(marker.get("class") or [])
        evidence, ambiguous = set(), False
        for raw in styles:
            for rule in parser.parseString(raw):
                if rule.type != rule.STYLE_RULE:
                    nested = str(rule.cssText)
                    if "vertical-align" in nested and any(
                            re.search(r"\." + re.escape(name) + r"(?![\w-])", nested) for name in classes):
                        ambiguous = True
                    continue
                alignment = rule.style.getPropertyValue("vertical-align")
                if not alignment:
                    continue
                for selector in rule.selectorText.split(","):
                    referenced = {name for name in classes if re.search(r"\." + re.escape(name) + r"(?![\w-])", selector)}
                    if not referenced:
                        continue
                    simple = re.fullmatch(r"(?:(\w+)\s*)?\.([\w-]+)", selector.strip())
                    if not simple:
                        ambiguous = True
                    elif simple.group(1) in (None, marker.name):
                        evidence.add(alignment.strip().lower())
        is_super = not ambiguous and evidence == {"super"}
    if numeric and not trailing and is_super:
        clone = copy.deepcopy(node)
        [child for child in clone.children if getattr(child, "name", None)][-1].decompose()
        return normalized_text(clone.get_text("", strip=False), opencc)
    return normalized_text(node.get_text("", strip=False), opencc)


class BookSnapshot:
    def __init__(self, path, opencc):
        from bs4 import BeautifulSoup
        import hashlib

        self.docs = {}
        self.images = Counter()
        self.navigation = []
        self.toc = []
        self.nav_docs = set()
        self.toc_docs = set()
        self.names = set()
        with zipfile.ZipFile(path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            opf = container.findall(".//{*}rootfile")[-1].attrib["full-path"]
            package = ET.fromstring(archive.read(opf))
            base = posixpath.dirname(opf)
            items = package.findall(".//{*}manifest/{*}item")
            rooted = any(posixpath.normpath(unquote(item.get("href", ""))).startswith("../") for item in items)

            def logical(physical):
                return physical if rooted else posixpath.relpath(physical, base or ".")

            def physical(href, referring):
                parsed = urlsplit(href or "")
                if parsed.scheme or parsed.netloc:
                    return None
                target = posixpath.normpath(posixpath.join(posixpath.dirname(referring), unquote(parsed.path))) if parsed.path else referring
                return target, unquote(parsed.fragment)

            def resolve(href, referring):
                target = physical(href, referring)
                return (logical(target[0]), target[1]) if target else None

            self.names = {logical(name) for name in archive.namelist()}
            doc_physical = {}
            nav_physical = []
            ncx_physical = []
            for item in items:
                target = physical(item.get("href", ""), opf)
                if not target:
                    continue
                name = target[0]
                if name not in archive.namelist():
                    continue
                media = item.get("media-type", "")
                if media.startswith("image/"):
                    self.images[hashlib.sha256(archive.read(name)).hexdigest()] += 1
                if media in {"application/xhtml+xml", "text/html"}:
                    key = logical(name)
                    doc_physical[key] = name
                    soup = BeautifulSoup(archive.read(name), "html.parser")
                    body = soup.find("body") or soup
                    ids = {str(node["id"]) for node in soup.find_all(id=True)}
                    headings = [(str(node.get("id") or ""), heading_evidence_label(node, soup, archive, name, opencc))
                                for node in body.find_all(re.compile(r"^h[1-6]$"))]
                    links = [{"label": normalized_text(node.get_text("", strip=False), opencc),
                              "target": resolve(node["href"], name), "href": node["href"],
                              "disabled": False}
                             for node in body.find_all("a", href=True)]
                    for link, node in zip(links, body.find_all("a", href=True)):
                        ancestors = [node, *node.parents]
                        types = {value for ancestor in ancestors
                                 for value in str(ancestor.get("epub:type") or "").split()}
                        roles = {value for ancestor in ancestors
                                 for value in str(ancestor.get("role") or "").split()}
                        link["in_toc"] = "toc" in types or "doc-toc" in roles
                        link["explicitly_protected"] = bool(
                            types & {"page-list", "noteref", "footnote", "endnote", "rearnote", "backlink"}
                            or roles & {"doc-pagelist", "doc-noteref", "doc-footnote", "doc-endnote", "doc-backlink"})
                    for node in body.find_all("a", attrs={"data-epub-factory-original-href": True}):
                        if node.has_attr("href"):
                            continue
                        href = node["data-epub-factory-original-href"]
                        links.append({"label": normalized_text(node.get_text("", strip=False), opencc),
                                      "target": resolve(href, name), "href": href, "disabled": True,
                                      "aria_disabled": node.get("aria-disabled")})
                    for nonprose in body.find_all(["script", "style"]):
                        nonprose.decompose()
                    self.docs[key] = {
                        "text": normalized_text(body.get_text("", strip=False), opencc),
                        "ids": ids, "headings": headings, "links": links,
                        "body_ids": {str(node["id"]) for node in body.find_all(id=True)},
                    }
                    if "nav" in item.get("properties", "").split():
                        self.nav_docs.add(key)
                        self.toc_docs.add(key)
                        nav_physical.append(name)
                elif media == "application/x-dtbncx+xml":
                    ncx_physical.append(name)

            for reference in package.findall(".//{*}guide/{*}reference"):
                guide_target = resolve(reference.get("href", ""), opf)
                self.navigation.append({"document": "<package-guide>",
                                        "label": normalized_text(reference.get("title") or reference.get("type") or "", opencc),
                                        "target": guide_target, "disabled": False, "guide_type": reference.get("type")})
                if reference.get("type") == "toc":
                    if guide_target:
                        self.toc_docs.add(guide_target[0])

            # Include navigation and the guide-designated HTML TOC, but not
            # ordinary prose links; these are tested independently below.
            for key, document in self.docs.items():
                document["protected_links"] = [
                    link for link in document["links"] if not link["disabled"] and (
                        link.get("explicitly_protected")
                        or (key not in self.toc_docs or key in self.nav_docs) and not link.get("in_toc"))]
            for key in self.toc_docs:
                for link in self.docs.get(key, {}).get("links", []):
                    self.navigation.append({"document": key, **link})

            # NCX is the primary source for these three historical books.
            # For NAV-only books, retain ordered nesting depth as well.
            if ncx_physical:
                root = ET.fromstring(archive.read(ncx_physical[0]))
                def walk_ncx(parent, depth):
                    for point in parent.findall("./{*}navPoint"):
                        label = point.find("./{*}navLabel/{*}text")
                        content = point.find("./{*}content")
                        if label is not None and content is not None:
                            record = {"label": normalized_text("".join(label.itertext()), opencc),
                                      "target": resolve(content.get("src", ""), ncx_physical[0]),
                                      "depth": depth, "disabled": False,
                                      "document": logical(ncx_physical[0])}
                            self.toc.append(record)
                            self.navigation.append(record)
                        walk_ncx(point, depth + 1)
                navmap = root.find("./{*}navMap")
                if navmap is not None:
                    walk_ncx(navmap, 0)
            elif nav_physical:
                soup = BeautifulSoup(archive.read(nav_physical[0]), "html.parser")
                for nav in soup.find_all("nav"):
                    if "toc" not in str(nav.get("epub:type") or "").split():
                        continue
                    for link in nav.find_all("a", href=True):
                        self.toc.append({"label": normalized_text(link.get_text("", strip=False), opencc),
                                         "target": resolve(link["href"], nav_physical[0]),
                                         "depth": len(link.find_parents("ol")) - 1,
                                         "disabled": False, "document": logical(nav_physical[0])})

    def valid(self, target):
        if target is None:
            return False
        name, fragment = target
        return name in self.names and (not fragment or fragment in self.docs.get(name, {}).get("ids", set()))


@unittest.skipUnless(
    os.environ.get("EPUB_HISTORY_UPLOAD_DIR") and os.environ.get("EPUB_HISTORY_OUTPUT_DIR"),
    "Explicit historical upload/output directories required; no synthetic replacement.",
)
class NavigationHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uploads = Path(os.environ["EPUB_HISTORY_UPLOAD_DIR"]).expanduser().resolve()
        cls.deliveries = Path(os.environ["EPUB_HISTORY_OUTPUT_DIR"]).expanduser().resolve()
        baseline = os.environ.get("EPUB_HISTORY_BASELINE_DIR")
        cls.baseline_root = Path(baseline).expanduser().resolve() if baseline else None
        cls.baseline_hashes = {}
        for book in BOOKS:
            for kind, directory in (("input", cls.uploads), ("output", cls.deliveries)):
                if sha256(directory / book[kind]) != book[kind + "_sha256"]:
                    raise AssertionError(f"Historical fixture changed: {book['key']} {kind}")
        cls.tmp = tempfile.TemporaryDirectory(prefix="epub-b1-history-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.stack.enter_context(patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(cls.root / "jobs.sqlite3"),
            "REPAIR_UPLOAD_DIR": str(cls.root / "repair"),
            "OPENAI_API_KEY": "", "ALIPAY_APP_ID": "", "SENTRY_DSN": "",
            "CELERY_BROKER_URL": "", "REDIS_URL": "", "NOTIFY_EMAIL_ENABLED": "0",
            "OWNER_PAYMENT_EMAIL_ENABLED": "0",
        }))
        cls.stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        cls.guards = [cls.stack.enter_context(patch(target, side_effect=AssertionError("B1 history regression forbids external calls")))
                      for target in ("socket.socket.connect", "socket.create_connection", "requests.sessions.Session.request")]
        from opencc import OpenCC
        from app.engine.compiler import EPUBCHECK_JAR, ExtremeCompiler
        cls.guards.append(cls.stack.enter_context(patch(
            "app.engine.cleaners.semantics_translator.SemanticsTranslator.__init__",
            side_effect=AssertionError("B1 regression must never construct a translator"))))
        cls.opencc = OpenCC("t2s")
        cls.runs = {}
        cls.validation_reports = {}
        original_run = subprocess.run

        def capture_validation(command, **kwargs):
            process = original_run(command, **kwargs)
            if command[:2] == ["java", "-jar"] and "--json" in command:
                report = Path(command[command.index("--json") + 1])
                if report.is_file():
                    cls.validation_reports[Path(command[3]).name] = json.loads(report.read_text())
            return process

        cls.stack.enter_context(patch("app.engine.epub_validation.subprocess.run", side_effect=capture_validation))
        from app.engine.epub_validation import validate_epub
        for book in BOOKS:
            source = cls.root / (book["key"] + "-source.epub")
            output = cls.root / (book["key"] + "-converted.epub")
            shutil.copyfile(cls.uploads / book["input"], source)
            if cls.baseline_root:
                original_baseline = cls.baseline_root / book["input_sha256"][:12] / "converted.epub"
                cls.baseline_hashes[original_baseline] = sha256(original_baseline)
                isolated_baseline = cls.root / (book["key"] + "-baseline.epub")
                shutil.copyfile(original_baseline, isolated_baseline)
                validate_epub(isolated_baseline, EPUBCHECK_JAR)
            with redirect_stdout(io.StringIO()):
                compiler = ExtremeCompiler(str(source), str(output), output_mode="simplified",
                                           enable_translation=False, lexicon_domains=[], enable_proper_noun=False)
                generated = compiler.run()
            if not generated or not output.is_file():
                raise AssertionError(f"Conversion failed for {book['key']}: {compiler.final_message}")
            cls.runs[book["key"]] = (BookSnapshot(source, cls.opencc), BookSnapshot(output, cls.opencc), compiler)

    @classmethod
    def tearDownClass(cls):
        for guard in cls.guards:
            guard.assert_not_called()
        for book in BOOKS:
            for kind, directory in (("input", cls.uploads), ("output", cls.deliveries)):
                if sha256(directory / book[kind]) != book[kind + "_sha256"]:
                    raise AssertionError(f"Read-only historical fixture changed: {book['key']} {kind}")
        for baseline, expected in cls.baseline_hashes.items():
            if sha256(baseline) != expected:
                raise AssertionError("Pre-B1 baseline artifact was modified")

    def test_body_images_ids_and_valid_references_are_preserved(self):
        for key, (before, after, compiler) in self.runs.items():
            with self.subTest(book=key):
                self.assertEqual(compiler.metrics.mode, "full", "A fallback is not a B1 full-pipeline pass")
                self.assertEqual(before.images, after.images, "Original image bytes/count changed")
                for name, source in before.docs.items():
                    self.assertIn(name, after.docs, "Original document disappeared")
                    target = after.docs[name]
                    if name in before.nav_docs:
                        # Additional generated landmarks are allowed, but the
                        # source NAV is still user content: keep its visible
                        # text order, anchors and valid links like any XHTML.
                        cursor = iter(target["text"])
                        self.assertTrue(all(any(candidate == char for candidate in cursor)
                                            for char in source["text"]),
                                        f"Original NAV text disappeared/reordered: {name}")
                    else:
                        self.assertTrue(source["text"] == target["text"],
                                        f"Normalized prose changed: {name} ({len(source['text'])} -> {len(target['text'])} chars)")
                    self.assertLessEqual(source["ids"], target["ids"], f"Existing anchors disappeared: {name}")
                    old_links = Counter((link["label"], link["target"]) for link in source["links"] if before.valid(link["target"]))
                    new_links = Counter((link["label"], link["target"]) for link in target["links"] if after.valid(link["target"]) and not link["disabled"])
                    self.assertFalse(old_links - new_links, f"Existing valid links/footnotes regressed: {name}")

    def test_valid_toc_entries_keep_labels_targets_order_and_depth(self):
        for key, (before, after, _) in self.runs.items():
            with self.subTest(book=key):
                expected = [(entry["label"], entry["target"], entry["depth"]) for entry in before.toc if before.valid(entry["target"])]
                actual = [(entry["label"], entry["target"], entry["depth"]) for entry in after.toc if after.valid(entry["target"])]
                self.assertTrue(expected, "Historical fixture has no valid TOC coverage")
                cursor = iter(actual)
                missing = [entry for entry in expected if entry not in actual]
                self.assertTrue(all(any(candidate == entry for candidate in cursor) for entry in expected),
                                f"Original valid TOC was changed or reordered: {key}; "
                                f"missing {len(missing)}/{len(expected)}; first {missing[:3]!r}")

    def test_page_lists_and_prose_note_links_are_never_retargeted(self):
        for key, (before, after, _) in self.runs.items():
            for name, document in before.docs.items():
                with self.subTest(book=key, document=name):
                    def signature(link):
                        return (link["label"], link["target"] if link["target"] is not None else link["href"])
                    original = Counter(signature(link) for link in document["protected_links"])
                    if not original:
                        continue
                    self.assertIn(name, after.docs)
                    current = Counter(signature(link) for link in after.docs[name]["protected_links"])
                    self.assertFalse(original - current, "Page-list, ordinary prose or note/backlink was retargeted/dropped")

    def test_changed_bad_navigation_has_unique_original_evidence(self):
        repaired = 0
        for key, (before, after, _) in self.runs.items():
            for entry in before.navigation:
                if entry["target"] is None or before.valid(entry["target"]):
                    continue
                with self.subTest(book=key, document=entry["document"], target=entry["target"]):
                    candidates = [candidate for candidate in after.navigation
                                  if candidate["label"] == entry["label"] and candidate["document"] == entry["document"]]
                    self.assertTrue(candidates, "Broken navigation label/structure disappeared")
                    name, fragment = entry["target"]
                    valid = {candidate["target"] for candidate in candidates if not candidate["disabled"] and after.valid(candidate["target"])}
                    if not valid:
                        # A still-broken source link is not claimed repaired. An
                        # explicitly disabled missing target must retain provenance.
                        disabled = [candidate for candidate in candidates if candidate["disabled"]]
                        if disabled:
                            self.assertNotIn(name, before.names)
                            self.assertRegex(posixpath.basename(name), r"^[xX]{3,}$")
                            self.assertTrue(any(candidate["target"] == entry["target"] for candidate in disabled))
                            self.assertTrue(all(candidate.get("aria_disabled") == "true" for candidate in disabled))
                        continue
                    self.assertEqual(len(valid), 1, "Repair has multiple plausible targets")
                    target_name, target_fragment = next(iter(valid))
                    self.assertEqual(target_name, name, "Do not invent a different chapter target")
                    source = before.docs.get(name)
                    self.assertIsNotNone(source, "Missing chapters cannot be guessed")
                    matching = [(anchor, text) for anchor, text in source["headings"] if text == entry["label"]]
                    exact_heading = len(matching) == 1 and (
                        matching[0][0] == target_fragment
                        or not matching[0][0] and (target_fragment, entry["label"]) in after.docs[name]["headings"])
                    cross_proved = set()
                    for other in before.navigation:
                        if other["document"] == "<package-guide>" or other["target"] != entry["target"]:
                            continue
                        matches = [anchor for anchor, text in source["headings"] if anchor and text == other["label"]]
                        if len(matches) == 1:
                            cross_proved.add(matches[0])
                    reading_start = (
                        entry.get("guide_type") in {"text", "bodymatter"}
                        and entry["label"].casefold() in {"start", "beginning", "bodymatter", "正文", "开始"}
                        and len(source["headings"]) == 1
                        and source["body_ids"] == {source["headings"][0][0]}
                        and target_fragment == source["headings"][0][0]
                        and source["text"].startswith(source["headings"][0][1]))
                    has_evidence = (
                        exact_heading or reading_start
                        or entry["document"] == "<package-guide>" and cross_proved == {target_fragment}
                        or entry.get("guide_type") == "toc" and name in before.toc_docs and not source["body_ids"] and not target_fragment
                    )
                    self.assertTrue(has_evidence, "Repair lacks a unique source heading/anchor or confirmed TOC")
                    repaired += 1
        self.assertGreater(repaired, 0, "No historical broken-target repair was exercised")

    def test_actual_epubcheck_fixes_target_without_new_errors_elsewhere(self):
        if self.baseline_root is None:
            self.skipTest("EPUB_HISTORY_BASELINE_DIR is required to compare against pre-B1 conversion, not the original EPUB version")
        for key, (_, _, compiler) in self.runs.items():
            with self.subTest(book=key):
                self.assertIsNotNone(compiler._epubcheck_result)
                self.assertIn(key + "-baseline.epub", self.validation_reports)
                self.assertIn(key + "-converted.epub", self.validation_reports)
                before = self.validation_reports[key + "-baseline.epub"]
                after = self.validation_reports[key + "-converted.epub"]
                if key == "responsibility-and-judgement":
                    self.assertTrue(compiler.validation_passed, compiler.final_message)
                    self.assertEqual(after["checker"]["nError"], 0)
                    self.assertEqual(after["checker"]["nFatal"], 0)
                else:
                    def failures(report):
                        return Counter((message.get("ID") or message.get("id"), message["severity"])
                                       for message in report["messages"] if message["severity"] in {"ERROR", "FATAL"})
                    self.assertFalse(failures(after) - failures(before), "B1 introduced new EPUBCheck error categories/counts")


if __name__ == "__main__":
    unittest.main(verbosity=2)
