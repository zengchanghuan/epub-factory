"""
D8 测试：Reduce 回写章节

- get_node_by_locator 能按路径解析并定位节点
- apply_chunk_results 单语模式替换原文为译文
- apply_chunk_results 双语模式保留原文并在其后插入译文（epub-original / epub-translated）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bs4 import BeautifulSoup
from app.engine.chunk_extractor import extract_chunks
from app.domain.chapter_reduce_service import get_node_by_locator, apply_chunk_results
from app.domain.chapter_translation_service import ChunkResult


def test_get_node_by_locator():
    html = """<!DOCTYPE html><html><body><div><p>First</p><p>Second</p></div></body></html>"""
    soup = BeautifulSoup(html, "html.parser")
    # 与 chunk_extractor 一致：从 HTML 提取 locator
    chunks = extract_chunks(html.encode("utf-8"), "ch1")
    assert len(chunks) >= 1
    loc1 = chunks[0].locator
    node = get_node_by_locator(soup, loc1)
    assert node is not None
    assert node.name == "p"
    assert "First" in node.get_text()


def test_get_node_by_locator_invalid_returns_none():
    html = "<html><body><p>X</p></body></html>"
    soup = BeautifulSoup(html, "html.parser")
    assert get_node_by_locator(soup, "") is None
    assert get_node_by_locator(soup, "/html/body/p[99]") is None


def test_apply_chunk_results_monolingual():
    html = """<!DOCTYPE html><html><body><div><p>Hello</p><p>World</p></div></body></html>"""
    raw = html.encode("utf-8")
    chunks = extract_chunks(raw, "ch1")
    assert len(chunks) >= 2
    results = [
        ChunkResult(
            chunk_id=chunks[0].chunk_id,
            sequence=chunks[0].sequence,
            locator=chunks[0].locator,
            original_html=chunks[0].html,
            translated_html="<p>你好</p>",
            cached=False,
        ),
        ChunkResult(
            chunk_id=chunks[1].chunk_id,
            sequence=chunks[1].sequence,
            locator=chunks[1].locator,
            original_html=chunks[1].html,
            translated_html="<p>世界</p>",
            cached=False,
        ),
    ]
    out = apply_chunk_results(raw, results, bilingual=False)
    text = out.decode("utf-8")
    assert "你好" in text
    assert "世界" in text
    assert "Hello" not in text
    assert "World" not in text


def test_apply_chunk_results_bilingual():
    html = """<!DOCTYPE html><html><body><div><p>Hello</p></div></body></html>"""
    raw = html.encode("utf-8")
    chunks = extract_chunks(raw, "ch1")
    assert len(chunks) >= 1
    results = [
        ChunkResult(
            chunk_id=chunks[0].chunk_id,
            sequence=chunks[0].sequence,
            locator=chunks[0].locator,
            original_html=chunks[0].html,
            translated_html="<p>你好</p>",
            cached=False,
        ),
    ]
    out = apply_chunk_results(raw, results, bilingual=True)
    text = out.decode("utf-8")
    assert "Hello" in text
    assert "你好" in text
    assert "epub-original" in text
    assert "epub-translated" in text


def _run():
    cases = [
        test_get_node_by_locator,
        test_get_node_by_locator_invalid_returns_none,
        test_apply_chunk_results_monolingual,
        test_apply_chunk_results_bilingual,
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
