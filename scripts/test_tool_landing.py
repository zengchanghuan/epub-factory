"""Static SEO pages must never regenerate a competing transaction client."""
from contextlib import ExitStack
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import generate_seo_pages as landing


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))

    def nodes_for(self, tag):
        return [attrs for name, attrs in self.nodes if name == tag]


class ToolLandingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="epub-r11-landing-")))
        self.output = self.root / "output"
        self.network = [self.stack.enter_context(patch(target, side_effect=AssertionError("Offline generator")))
                        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo")]

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    def generate(self):
        self.assertTrue(landing.generate_seo_pages(output_dir=self.output))
        return {page["filename"]: (self.output / page["filename"]).read_text(encoding="utf-8")
                for page in landing.PAGES}

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.stat().st_ino, path.stat().st_mtime_ns,
                                                  path.read_bytes() if path.is_file() else None)
                for path in self.root.rglob("*")}

    def cli(self, *arguments, script=None):
        return subprocess.run([sys.executable, str(script or Path(landing.__file__)), *arguments],
                              capture_output=True, text=True, timeout=20)

    def test_generation_is_deterministic_and_checked_without_writes(self):
        first = self.generate()
        second = self.generate()
        self.assertEqual(first, second)
        before = self.snapshot()
        self.assertTrue(landing.generate_seo_pages(check=True, output_dir=self.output))
        result = self.cli("--check", "--output-dir", str(self.output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_check_missing_or_stale_outputs_fails_without_creating_or_rewriting_them(self):
        missing = self.root / "not-created"
        result = self.cli("--check", "--output-dir", str(missing))
        self.assertEqual(result.returncode, 1)
        self.assertIn("out of date", result.stdout)
        self.assertFalse(missing.exists())
        self.generate()
        stale = self.output / landing.PAGES[0]["filename"]
        stale.write_text("stale description", encoding="utf-8")
        before = self.snapshot()
        self.assertFalse(landing.generate_seo_pages(check=True, output_dir=self.output))
        result = self.cli("--check", "--output-dir", str(self.output))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.snapshot(), before)

    def test_standalone_generator_only_needs_dedicated_template_not_index(self):
        scripts = self.root / "isolated-project" / "scripts"
        (scripts / "templates").mkdir(parents=True)
        isolated = scripts / "generate_seo_pages.py"
        shutil.copyfile(landing.__file__, isolated)
        shutil.copyfile(landing.TEMPLATE_PATH, scripts / "templates" / "tool-landing.html")
        # Deliberately unreadable as a file: copying the application must fail.
        (scripts.parent / "frontend" / "index.html").mkdir(parents=True)
        generated = self.cli(script=isolated)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        checked = self.cli("--check", script=isolated)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(len(list((scripts.parent / "frontend").glob("*.html"))), 4)
        self.assertTrue((scripts.parent / "frontend" / "index.html").is_dir())

    def test_each_page_has_native_same_origin_entry_and_task_fallback(self):
        generated = self.generate()
        for config in landing.PAGES:
            with self.subTest(tool=config["tool"]):
                html = generated[config["filename"]]
                page = Page(html)
                self.assertEqual(page.nodes_for("body"), [{"data-tool-entry": config["tool"]}])
                uploads = [node for node in page.nodes_for("a") if node.get("data-entry-link") == "upload"]
                tasks = [node for node in page.nodes_for("a") if node.get("data-entry-link") == "tasks"]
                self.assertGreaterEqual(len(uploads), 1)
                self.assertGreaterEqual(len(tasks), 1)
                self.assertTrue(all(node["href"] == "./?tool=" + config["tool"] for node in uploads))
                self.assertTrue(all(node["href"] == "./?view=tasks" for node in tasks))
                self.assertIn("<noscript>", html)
                self.assertIn("原浏览器的任务中心恢复任务", html)
                self.assertIn("无需重新上传或重复付款", html)
                self.assertFalse(any(node.get("target") == "_blank" for node in uploads + tasks))

    def test_static_pages_have_no_upload_payment_status_or_download_engine(self):
        for name, html in self.generate().items():
            with self.subTest(page=name):
                page = Page(html)
                self.assertFalse(page.nodes_for("form"))
                self.assertFalse(page.nodes_for("input"))
                self.assertFalse(page.nodes_for("button"))
                self.assertFalse(re.search(r"fetch\s*\(|FormData\s*\(|XMLHttpRequest|EventSource|setInterval|paypal|/api/v[12]/", html, re.I))
                scripts = page.nodes_for("script")
                self.assertEqual([node.get("src") for node in scripts if node.get("src")],
                                 ["tool-entry.js?v=20261001-r11"])
                self.assertTrue(all(node.get("type") == "application/ld+json" or
                                    (node.get("src") and "defer" in node) for node in scripts))
                self.assertNotIn("lib.js", html)
                self.assertNotIn("epub.min.js", html)

    def test_page_specific_canonical_schema_and_accessible_content(self):
        titles = set()
        generated = self.generate()
        for config in landing.PAGES:
            html = generated[config["filename"]]
            page = Page(html)
            canonical = "https://fixepub.com/" + config["filename"]
            self.assertEqual([node["href"] for node in page.nodes_for("link") if node.get("rel") == "canonical"], [canonical])
            schema = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)[1])
            self.assertEqual(schema["@type"], "WebPage")
            self.assertEqual(schema["url"], canonical)
            self.assertEqual(schema["name"], config["title"])
            self.assertEqual(schema["description"], config["description"])
            self.assertEqual(len(page.nodes_for("h1")), 1)
            self.assertEqual(len(page.nodes_for("details")), 3)
            self.assertNotIn("offers", schema)
            self.assertNotIn("aggregateRating", schema)
            # Responsive CSS percentages are layout values, not marketing claims.
            content = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
            for claim in ("免费", "99.8%", "10,000+", "$1.99", "100%", "GPT-4", "Claude"):
                self.assertNotIn(claim, content)
            for link in ("/contact.html", "/privacy.html", "/terms.html", "/refund.html",
                         "mailto:249998620@qq.com", "https://beian.miit.gov.cn/"):
                self.assertIn(link, [node.get("href") for node in page.nodes_for("a")])
            self.assertIn("粤ICP备2026051457号-1", html)
            titles.add(schema["name"])
        self.assertEqual(len(titles), 3)

    def test_formats_pdf_gate_and_current_pricing_are_not_reintroduced_as_free(self):
        generated = self.generate()
        for html in generated.values():
            self.assertIn("暂不支持 PDF", html)
            self.assertIn("本页不会接收文件或创建收费订单", html)
            for extension in (".epub", ".docx", ".md", ".markdown"):
                self.assertIn(extension, html)
        for filename in ("vertical-to-horizontal.html", "traditional-to-simplified.html"):
            html = generated[filename]
            self.assertIn("<strong>¥0.99 / 本</strong>", html)
            self.assertIn("AI 精校不包含在基础价中", html)
            self.assertIn(".mobi", html)
            self.assertIn(".azw3", html)
        translator = generated["epub-translator.html"]
        self.assertIn("AI 翻译按主页当前报价收费", translator)
        self.assertNotIn("¥0.99", translator)
        self.assertIn("MOBI / AZW3 不提供直接翻译", translator)


if __name__ == "__main__":
    unittest.main(verbosity=2)
