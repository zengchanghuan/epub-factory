"""
D6 测试：Manifest 设计与正文识别

- classify_chapter_kind 正确区分 body/nav/copyright/appendix/index/other
- extract_chunks 提取块级元素并生成稳定 locator
- build_manifest 返回标准结构 job_id + chapters[]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.domain.manifest_service import classify_chapter_kind, build_manifest
from app.models import ChapterKind
from app.engine.chunk_extractor import extract_chunks, ChunkItem


def test_classify_body():
    assert classify_chapter_kind("06_Understanding_Energy.xhtml") == ChapterKind.body
    assert classify_chapter_kind("chapter_01.xhtml") == ChapterKind.body
    assert classify_chapter_kind("part1.xhtml") == ChapterKind.body


def test_classify_nav():
    assert classify_chapter_kind("nav.xhtml") == ChapterKind.nav
    assert classify_chapter_kind("toc.xhtml") == ChapterKind.nav
    assert classify_chapter_kind("contents.xhtml") == ChapterKind.nav


def test_classify_copyright():
    assert classify_chapter_kind("copyright.xhtml") == ChapterKind.copyright
    assert classify_chapter_kind("license.xhtml") == ChapterKind.copyright
    assert classify_chapter_kind("colophon.xhtml") == ChapterKind.copyright


def test_classify_appendix_index_other():
    assert classify_chapter_kind("appendix_a.xhtml") == ChapterKind.appendix
    assert classify_chapter_kind("index.xhtml") == ChapterKind.index
    assert classify_chapter_kind("cover.xhtml") == ChapterKind.other
    assert classify_chapter_kind("about_the_author.xhtml") == ChapterKind.other


def test_extract_chunks_returns_locator_and_sequence():
    html = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
  <div class="main">
    <p>First paragraph.</p>
    <p>Second paragraph.</p>
  </div>
</body>
</html>"""
    chunks = extract_chunks(html, "chap_01")
    assert len(chunks) == 2
    assert all(isinstance(c, ChunkItem) for c in chunks)
    assert chunks[0].sequence == 1
    assert chunks[1].sequence == 2
    assert chunks[0].chunk_id == "chap_01_0001"
    assert chunks[1].chunk_id == "chap_01_0002"
    assert chunks[0].locator.startswith("/")
    assert "p[" in chunks[0].locator or "div[" in chunks[0].locator
    assert chunks[0].text == "First paragraph."
    assert chunks[1].word_count >= 2
    assert chunks[1].char_count >= 2


def test_extract_chunks_skips_nested_blocks():
    html = b"""<html><body>
    <div><p>Leaf only.</p></div>
</body></html>"""
    chunks = extract_chunks(html, "c1")
    assert len(chunks) == 1
    assert chunks[0].text.strip() == "Leaf only."


def test_build_manifest_structure():
    # 使用不存在的路径，应返回 error 或空 chapters
    out = build_manifest("/nonexistent/book.epub", "job_abc")
    assert "job_id" in out
    assert out["job_id"] == "job_abc"
    assert "chapters" in out
    assert isinstance(out["chapters"], list)
    if not out.get("error"):
        for ch in out["chapters"]:
            assert "chapter_id" in ch
            assert "file_path" in ch
            assert "chapter_kind" in ch
            assert "chunks" in ch


def test_build_manifest_with_real_epub_if_present():
    test_epub = Path(__file__).parent / "test_en.epub"
    if not test_epub.exists():
        return
    out = build_manifest(str(test_epub), "job_test")
    assert "error" not in out
    assert len(out["chapters"]) >= 1
    body_chapters = [c for c in out["chapters"] if c["chapter_kind"] == "body"]
    if body_chapters:
        first = body_chapters[0]
        assert "chunks" in first
        assert isinstance(first["chunks"], list)


if __name__ == "__main__":
    tests = [
        test_classify_body,
        test_classify_nav,
        test_classify_copyright,
        test_classify_appendix_index_other,
        test_extract_chunks_returns_locator_and_sequence,
        test_extract_chunks_skips_nested_blocks,
        test_build_manifest_structure,
        test_build_manifest_with_real_epub_if_present,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  ✅ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n📊 {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
