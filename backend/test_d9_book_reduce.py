"""
D9 测试：全书 Reduce 与打包

- set_chapter_output / get_chapter_output 读写章节回写结果
- reduce_and_package 在无 EPUB 时返回 False
- reduce_and_package 对有效 EPUB + 章节内容回调能打包出结果
"""

import tempfile
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ebooklib import epub
from app.domain.book_reduce_service import (
    set_chapter_output,
    get_chapter_output,
    make_get_chapter_content,
    reduce_and_package,
)
from app.engine.packager import EpubPackager
from app.engine.toc_rebuilder import TocRebuilder


def test_set_and_get_chapter_output():
    job_id = "d9_test_job_1"
    file_path = "OEBPS/chap.xhtml"
    content = b"<html><body><p>Reduced</p></body></html>"
    set_chapter_output(job_id, file_path, content)
    out = get_chapter_output(job_id, file_path)
    assert out == content
    assert get_chapter_output(job_id, "nonexistent.xhtml") is None


def test_make_get_chapter_content():
    job_id = "d9_test_job_2"
    file_path = "c1.xhtml"
    content = b"<p>OK</p>"
    set_chapter_output(job_id, file_path, content)
    getter = make_get_chapter_content(job_id)
    assert getter(file_path) == content
    assert getter("other.xhtml") is None


def test_reduce_and_package_invalid_path_returns_false():
    ok = reduce_and_package(
        "/nonexistent/book.epub",
        "/tmp/out.epub",
        lambda _: b"<p>x</p>",
    )
    assert ok is False


def test_reduce_and_package_with_minimal_epub():
    """创建最小 EPUB，不覆盖任何章节（回调均返回 None），应能完成 TOC+打包。"""
    with tempfile.TemporaryDirectory() as tmp:
        inp = Path(tmp) / "in.epub"
        out = Path(tmp) / "out.epub"
        book = epub.EpubBook()
        book.set_identifier("d9-minimal")
        book.set_title("D9 Minimal")
        book.set_language("en")
        c1 = epub.EpubHtml(title="Ch1", file_name="chap_01.xhtml", lang="en")
        c1.content = "<html><body><h1>Ch1</h1><p>Hello.</p></body></html>"
        book.add_item(c1)
        book.spine.append(c1)
        book.toc = (epub.Link("chap_01.xhtml", "Ch1", "ch1"),)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(str(inp), book, {})
        ok = reduce_and_package(
            str(inp),
            str(out),
            lambda _: None,
        )
        assert ok is True
        assert out.is_file()
        assert out.stat().st_size > 100


def test_reduce_and_package_overrides_chapter():
    """创建最小 EPUB，用回调覆盖一章内容，输出中应包含覆盖后的内容。"""
    with tempfile.TemporaryDirectory() as tmp:
        inp = Path(tmp) / "in.epub"
        out = Path(tmp) / "out.epub"
        book = epub.EpubBook()
        book.set_identifier("d9-override")
        book.set_title("D9 Override")
        book.set_language("en")
        c1 = epub.EpubHtml(title="Ch1", file_name="chap_01.xhtml", lang="en")
        c1.content = "<html><body><h1>Ch1</h1><p>Original</p></body></html>"
        book.add_item(c1)
        book.spine.append(c1)
        book.toc = (epub.Link("chap_01.xhtml", "Ch1", "ch1"),)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(str(inp), book, {})

        replacement = b"<html><body><h1>Ch1</h1><p>REPLACED_BY_REDUCE</p></body></html>"

        def get_content(file_path):
            if file_path == "chap_01.xhtml":
                return replacement
            return None

        ok = reduce_and_package(str(inp), str(out), get_content)
        assert ok is True
        assert out.is_file()
        book_out = epub.read_epub(str(out))
        for item in book_out.get_items():
            if item.get_type() == 9 and item.get_name() == "chap_01.xhtml":
                raw = item.get_content()
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                assert b"REPLACED_BY_REDUCE" in raw
                break
        else:
            assert False, "chap_01.xhtml not found in output"


def test_reduce_and_package_syncs_translated_toc_files():
    """章节标题被翻译后，实体 nav.xhtml / toc.ncx 也应同步为译文目录。"""
    with tempfile.TemporaryDirectory() as tmp:
        inp = Path(tmp) / "in.epub"
        out = Path(tmp) / "out.epub"
        book = epub.EpubBook()
        book.set_identifier("d9-toc-sync")
        book.set_title("D9 Toc Sync")
        book.set_language("en")
        c1 = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
        c1.content = "<html><body><h1>Chapter 1</h1><p>Original</p></body></html>"
        book.add_item(c1)
        book.spine.append(c1)
        book.toc = (epub.Link("chap_01.xhtml", "Chapter 1", "ch1"),)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(str(inp), book, {})

        replacement = "<html><body><h1>第一章</h1><p>已翻译</p></body></html>".encode("utf-8")

        ok = reduce_and_package(
            str(inp),
            str(out),
            lambda file_path: replacement if file_path == "chap_01.xhtml" else None,
        )

        assert ok is True
        with zipfile.ZipFile(out, "r") as zf:
            files = zf.namelist()
            nav_name = next(name for name in files if name.endswith("nav.xhtml"))
            ncx_name = next(name for name in files if name.endswith("toc.ncx"))
            nav_text = zf.read(nav_name).decode("utf-8", errors="ignore")
            ncx_text = zf.read(ncx_name).decode("utf-8", errors="ignore")

        assert "第一章" in nav_text
        assert "Chapter 1" not in nav_text
        assert "第一章" in ncx_text
        assert "Chapter 1" not in ncx_text


def test_packager_syncs_stale_serialized_toc_files():
    """阅读器读取旧 nav/ncx 时，打包后处理应按 book.toc 同步标题。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        oebps = root / "OEBPS"
        oebps.mkdir()
        (oebps / "nav.xhtml").write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<nav epub:type="toc"><ol><li><a href="xhtml/chap_01.xhtml#h1">Chapter 1</a></li></ol></nav>
</body></html>""",
            encoding="utf-8",
        )
        (oebps / "toc.ncx").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint id="h1" playOrder="1"><navLabel><text>Chapter 1</text></navLabel><content src="xhtml/chap_01.xhtml#h1"/></navPoint>
</navMap></ncx>""",
            encoding="utf-8",
        )

        book = epub.EpubBook()
        book.toc = (epub.Link("OEBPS/xhtml/chap_01.xhtml#h1", "第一章", "h1"),)
        packager = EpubPackager(book, root / "out.epub")

        assert packager._sync_serialized_toc_files(root) is True
        assert "第一章" in (oebps / "nav.xhtml").read_text(encoding="utf-8")
        assert "Chapter 1" not in (oebps / "nav.xhtml").read_text(encoding="utf-8")
        assert "第一章" in (oebps / "toc.ncx").read_text(encoding="utf-8")
        assert "Chapter 1" not in (oebps / "toc.ncx").read_text(encoding="utf-8")


def test_toc_rebuilder_preserves_existing_hierarchy_and_localizes_titles():
    """已有目录必须保留层级和 href，只更新高置信度译名。"""
    book = epub.EpubBook()
    chapter = epub.EpubHtml(title="Chapter", file_name="chapter.xhtml", lang="zh-CN")
    chapter.content = '<html><body><h1 id="c1">第一章</h1></body></html>'
    notes = epub.EpubHtml(title="Notes", file_name="notes.xhtml", lang="zh-CN")
    notes.content = '<html><body><p id="n9">脚注内容</p></body></html>'
    book.add_item(chapter)
    book.add_item(notes)
    book.toc = (
        epub.Link("chapter.xhtml#c1", "Chapter 1", "c1"),
        (
            epub.Section("Footnotes", "notes.xhtml"),
            [epub.Link("notes.xhtml#n9", "Page 9", "n9")],
        ),
    )

    rebuilt = TocRebuilder().rebuild(book)

    assert len(rebuilt.toc) == 2
    assert rebuilt.toc[0].title == "第一章"
    assert rebuilt.toc[0].href == "chapter.xhtml#c1"
    section, children = rebuilt.toc[1]
    assert section.title == "脚注"
    assert section.href == "notes.xhtml"
    assert children[0].title == "第9页"
    assert children[0].href == "notes.xhtml#n9"


def test_toc_rebuilder_does_not_localize_common_titles_for_non_chinese_target():
    """非中文目标语言不能因正文偶有中文而把通用目录名改成中文。"""
    book = epub.EpubBook()
    chapter = epub.EpubHtml(title="Introduction", file_name="chapter.xhtml", lang="en")
    chapter.content = '<html><body><h1 id="c1">Introduction 中文引文</h1></body></html>'
    book.add_item(chapter)
    book.toc = (epub.Link("chapter.xhtml#c1", "Introduction", "c1"),)

    rebuilt = TocRebuilder().rebuild(book, target_lang="fr")

    assert rebuilt.toc[0].title == "Introduction 中文引文"


def test_packager_prefers_fragment_specific_toc_titles():
    """同一文件的多个锚点不能都被最后一个标题覆盖。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "nav.xhtml").write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><ol>
<li><a href="chapter.xhtml#one">Old one</a></li>
<li><a href="chapter.xhtml#two">Old two</a></li>
</ol></nav></body></html>""",
            encoding="utf-8",
        )
        (root / "toc.ncx").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint id="one"><navLabel><text>Old one</text></navLabel><content src="chapter.xhtml#one"/></navPoint>
<navPoint id="two"><navLabel><text>Old two</text></navLabel><content src="chapter.xhtml#two"/></navPoint>
</navMap></ncx>""",
            encoding="utf-8",
        )
        book = epub.EpubBook()
        book.toc = (
            epub.Link("chapter.xhtml#one", "第一节", "one"),
            epub.Link("chapter.xhtml#two", "第二节", "two"),
        )

        assert EpubPackager(book, root / "out.epub")._sync_serialized_toc_files(root) is True
        nav = (root / "nav.xhtml").read_text(encoding="utf-8")
        ncx = (root / "toc.ncx").read_text(encoding="utf-8")
        assert "第一节" in nav and "第二节" in nav
        assert "第一节" in ncx and "第二节" in ncx


def test_packager_restores_svg_namespace_in_html_cover():
    """ebooklib 丢失 SVG 命名空间时，.html 封面也必须在后处理中修复。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "mimetype").write_text("application/epub+zip", encoding="utf-8")
        cover = root / "EPUB" / "cover.html"
        cover.parent.mkdir()
        cover.write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head/>
<body><svg:svg><svg:path d="M0 0"><svg:path d="M1 1"></svg:path></svg:path></svg:svg></body></html>""",
            encoding="utf-8",
        )

        output = root / "out.epub"
        packager = EpubPackager(epub.EpubBook(), output)
        packager._repack(root)
        packager._post_fix()

        with zipfile.ZipFile(output) as zf:
            repaired = zf.read("EPUB/cover.html").decode("utf-8")
        assert 'xmlns:svg="http://www.w3.org/2000/svg"' in repaired
        assert "<head><title>cover</title></head>" in repaired
        assert repaired.count("<svg:path") == 2
        assert repaired.count("<svg:path") == repaired.count("/>")
        assert "</svg:path>" not in repaired


def test_packager_restores_default_namespace_for_unprefixed_svg():
    """二次序列化后的 <svg>/<path> 也必须回到 SVG 命名空间。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "mimetype").write_text("application/epub+zip", encoding="utf-8")
        cover = root / "EPUB" / "cover.xhtml"
        cover.parent.mkdir()
        cover.write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head/>
<body><svg><path d="M0 0"><path d="M1 1"></path></path></svg></body></html>""",
            encoding="utf-8",
        )
        output = root / "out.epub"
        packager = EpubPackager(epub.EpubBook(), output)
        packager._repack(root)
        packager._post_fix()

        with zipfile.ZipFile(output) as zf:
            repaired = zf.read("EPUB/cover.xhtml").decode("utf-8")
        assert '<svg xmlns="http://www.w3.org/2000/svg">' in repaired
        assert repaired.count("<path") == 2
        assert repaired.count("<path") == repaired.count("/>")
        assert "</path>" not in repaired


def test_packager_adds_required_epub3_navigation():
    """ebooklib 固定输出 EPUB 3，缺少 nav 文件时必须自动补齐。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / "out.epub"
        book = epub.EpubBook()
        book.set_identifier("d9-nav")
        book.set_title("D9 Nav")
        book.set_language("en")
        chapter = epub.EpubHtml(title="Chapter", file_name="chapter.xhtml", lang="en")
        chapter.content = "<html><body><h1>Chapter</h1></body></html>"
        book.add_item(chapter)
        book.add_item(epub.EpubNcx())
        book.spine.append(chapter)
        book.toc = (epub.Link("chapter.xhtml", "Chapter", "chapter"),)

        assert EpubPackager(book, output).save() is True

        with zipfile.ZipFile(output) as zf:
            opf_name = next(name for name in zf.namelist() if name.endswith(".opf"))
            opf = zf.read(opf_name).decode("utf-8")
            assert any(name.endswith("nav.xhtml") for name in zf.namelist())
        assert 'properties="nav"' in opf


def _run():
    cases = [
        test_set_and_get_chapter_output,
        test_make_get_chapter_content,
        test_reduce_and_package_invalid_path_returns_false,
        test_reduce_and_package_with_minimal_epub,
        test_reduce_and_package_overrides_chapter,
        test_reduce_and_package_syncs_translated_toc_files,
        test_packager_syncs_stale_serialized_toc_files,
        test_toc_rebuilder_preserves_existing_hierarchy_and_localizes_titles,
        test_toc_rebuilder_does_not_localize_common_titles_for_non_chinese_target,
        test_packager_prefers_fragment_specific_toc_titles,
        test_packager_restores_svg_namespace_in_html_cover,
        test_packager_restores_default_namespace_for_unprefixed_svg,
        test_packager_adds_required_epub3_navigation,
    ]
    passed = 0
    for fn in cases:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {fn.__name__}: {e}")
            raise
    print(f"\n📊 {passed} passed, 0 failed")


if __name__ == "__main__":
    _run()
