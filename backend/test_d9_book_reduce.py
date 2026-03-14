"""
D9 测试：全书 Reduce 与打包

- set_chapter_output / get_chapter_output 读写章节回写结果
- reduce_and_package 在无 EPUB 时返回 False
- reduce_and_package 对有效 EPUB + 章节内容回调能打包出结果
"""

import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ebooklib import epub
from app.domain.book_reduce_service import (
    set_chapter_output,
    get_chapter_output,
    make_get_chapter_content,
    reduce_and_package,
)


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


def _run():
    cases = [
        test_set_and_get_chapter_output,
        test_make_get_chapter_content,
        test_reduce_and_package_invalid_path_returns_false,
        test_reduce_and_package_with_minimal_epub,
        test_reduce_and_package_overrides_chapter,
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
