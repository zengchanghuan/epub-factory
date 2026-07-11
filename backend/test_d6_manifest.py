"""
D6 测试：Manifest 设计与正文识别

- classify_chapter_kind 正确区分 body/nav/copyright/appendix/index/other
- extract_chunks 提取块级元素并生成稳定 locator
- build_manifest 返回标准结构 job_id + chapters[]
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.domain.manifest_service import (
    classify_chapter_kind,
    classify_chapter_kind_from_chunks,
    build_manifest,
)
from app.models import ChapterKind
from app.engine.chunk_extractor import extract_chunks, extract_chunks_with_stats, ChunkItem


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


def _chunk(text: str) -> ChunkItem:
    return ChunkItem(
        chunk_id="part_0001",
        sequence=1,
        locator="/html/body/p[1]",
        html=f"<p>{text}</p>",
        text=text,
        word_count=len(text.split()),
        char_count=len(text),
    )


def test_classify_generic_files_by_content_heading():
    assert classify_chapter_kind_from_chunks("text/part0109.html", [_chunk("Bibliography")]) == ChapterKind.index
    assert classify_chapter_kind_from_chunks("text/part0110.html", [_chunk("Sources")]) == ChapterKind.index
    assert classify_chapter_kind_from_chunks("text/part0148.html", [_chunk("Index")]) == ChapterKind.index
    assert classify_chapter_kind_from_chunks("text/part0111.html", [_chunk("Photo Credits")]) == ChapterKind.other


def test_content_heading_classification_is_exact():
    assert classify_chapter_kind_from_chunks("text/part0001.html", [_chunk("Sources of Inspiration")]) == ChapterKind.body


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


def test_extract_chunks_translates_image_captions_and_routes_footnotes():
    html = b"""<html><body>
    <p>Normal body paragraph.</p>
    <p class="footnoteg"><span><sup><a href="part0048.html#ch20fn7">7</a></sup> Crick, Pauling, and coiled-coils.</span></p>
    <p class="caption">A figure from Pauling and Corey's paper.</p>
    <p>Another body paragraph.</p>
</body></html>"""
    chunks, stats = extract_chunks_with_stats(html, "c1")
    assert [c.text for c in chunks] == [
        "Normal body paragraph.",
        "7 Crick, Pauling, and coiled-coils.",
        "A figure from Pauling and Corey's paper.",
        "Another body paragraph.",
    ]
    assert [c.chunk_id for c in chunks] == ["c1_0001", "c1_0002", "c1_0003", "c1_0004"]
    assert [c.sequence for c in chunks] == [1, 2, 3, 4]
    assert chunks[1].translation_strategy == "text_nodes"
    assert chunks[2].translation_strategy == "html"
    assert stats["image_note_chunks_skipped"] == 0
    assert stats["image_caption_chunks"] == 1
    assert stats["structured_note_chunks"] == 1


def test_extract_chunks_routes_explanatory_endnotes_to_text_nodes():
    html = b"""<html><body>
    <p>This body paragraph should still be translated.</p>
    <p class="endnotes" id="p0040-08-11"><sup><a href="#n2"><sup>2</sup></a></sup>
      <i>Cf. </i>the last sentence of his <i>Enquiry Concerning Human Understanding</i>.
    </p>
    <p>Another body paragraph.</p>
</body></html>"""
    chunks, stats = extract_chunks_with_stats(html, "c1")
    assert [" ".join(c.text.split()) for c in chunks] == [
        "This body paragraph should still be translated.",
        "2 Cf. the last sentence of his Enquiry Concerning Human Understanding.",
        "Another body paragraph.",
    ]
    assert [c.sequence for c in chunks] == [1, 2, 3]
    assert chunks[1].translation_strategy == "text_nodes"
    assert stats["structured_note_chunks"] == 1


def test_extract_chunks_keeps_body_paragraph_with_footnote_reference():
    html = b"""<html><body>
    <p class="noindent1">This body paragraph should be translated before delivery.
      <sup class="sup"><a id="ch01fn1"></a><a href="part0113.html#ch01fn1a">1</a></sup>
      It continues after the note marker.
    </p>
    <p class="footnoteg"><span><sup><a href="part0048.html#ch20fn7">7</a></sup> A footnote note block.</span></p>
    </body></html>"""
    chunks, stats = extract_chunks_with_stats(html, "c1")
    assert len(chunks) == 2
    assert chunks[0].sequence == 1
    assert chunks[0].chunk_id == "c1_0001"
    assert "should be translated" in chunks[0].text
    assert chunks[1].translation_strategy == "text_nodes"
    assert stats["structured_note_chunks"] == 1


def test_extract_chunks_keeps_reference_only_note_as_source():
    html = b"""<html><body>
    <p>Normal body paragraph.</p>
    <p class="footnote">1 Smith 2020 pages 10-12 doi:10.1234/example</p>
    </body></html>"""
    chunks, stats = extract_chunks_with_stats(html, "c1")
    assert [c.text for c in chunks] == ["Normal body paragraph."]
    assert stats["reference_note_chunks_skipped"] == 1


def test_extract_chunks_can_include_image_note_blocks_when_disabled():
    html = b"""<html><body>
    <p>Normal body paragraph.</p>
    <div class="figure"><img src="figure.jpg"/>Embedded image label.</div>
</body></html>"""
    old = os.environ.get("EPUB_SKIP_IMAGE_NOTE_CHUNKS")
    os.environ["EPUB_SKIP_IMAGE_NOTE_CHUNKS"] = "0"
    try:
        chunks, stats = extract_chunks_with_stats(html, "c1")
    finally:
        if old is None:
            os.environ.pop("EPUB_SKIP_IMAGE_NOTE_CHUNKS", None)
        else:
            os.environ["EPUB_SKIP_IMAGE_NOTE_CHUNKS"] = old
    assert len(chunks) == 2
    assert stats["image_note_chunks_skipped"] == 0


def test_extract_chunks_still_skips_blocks_containing_image_media():
    html = b"""<html><body>
    <p>Normal body paragraph.</p>
    <div class="figure"><img src="figure.jpg"/>Embedded image label.</div>
    </body></html>"""
    chunks, stats = extract_chunks_with_stats(html, "c1")
    assert [c.text for c in chunks] == ["Normal body paragraph."]
    assert stats["image_note_chunks_skipped"] == 1
    assert stats["image_caption_chunks"] == 0


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
        test_classify_generic_files_by_content_heading,
        test_content_heading_classification_is_exact,
        test_extract_chunks_returns_locator_and_sequence,
        test_extract_chunks_skips_nested_blocks,
        test_extract_chunks_translates_image_captions_and_routes_footnotes,
        test_extract_chunks_routes_explanatory_endnotes_to_text_nodes,
        test_extract_chunks_keeps_body_paragraph_with_footnote_reference,
        test_extract_chunks_keeps_reference_only_note_as_source,
        test_extract_chunks_can_include_image_note_blocks_when_disabled,
        test_extract_chunks_still_skips_blocks_containing_image_media,
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
