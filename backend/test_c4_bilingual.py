"""
C4 测试：双语对照输出（bilingual mode）

测试用例：
1. bilingual=False（默认）：译文替换原文，原文消失
2. bilingual=True：原文保留，译文插在原文之后
3. bilingual=True：原文段落含 class="epub-original"
4. bilingual=True：译文段落含 class="epub-translated"
5. 无需翻译的段落（纯数字/中文）在双语模式下保持不变
6. ExtremeCompiler bilingual 参数正确透传给 SemanticsTranslator
7. API Job 模型含 bilingual 字段，默认为 False
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.engine.compiler import ExtremeCompiler
from app.models import Job, OutputMode, DeviceProfile


# ─── 辅助：构造一个 mock 过 LLM 调用的翻译器 ─────────────────────────────────

def make_translator(bilingual: bool) -> SemanticsTranslator:
    t = SemanticsTranslator(target_lang="zh-CN", bilingual=bilingual)
    # mock _call_llm：将 <p>...</p> 中的英文替换为中文占位
    async def fake_llm(html_chunk: str) -> str:
        return html_chunk.replace("Hello world", "你好世界").replace(
            "Second paragraph", "第二段落"
        )
    t._call_llm = fake_llm
    return t


def run_async(coro):
    return asyncio.run(coro)


# ─── 单元测试 ─────────────────────────────────────────────────────────────────

def test_default_mode_replaces_original():
    print("\n" + "=" * 60)
    print("🧪 Test 1: 默认模式（bilingual=False）— 译文替换原文")
    print("=" * 60)

    t = make_translator(bilingual=False)
    html = b"<html><body><p>Hello world</p></body></html>"
    result = run_async(t.process_async(html, item_type=9)).decode()

    assert "你好世界" in result, "译文应出现在结果中"
    assert "Hello world" not in result, "原文应被替换"
    print(f"  结果片段: {result[result.find('<p'):][:60]}")
    print("  ✅ PASS: 默认模式替换正常")
    return True


def test_bilingual_keeps_original():
    print("\n" + "=" * 60)
    print("🧪 Test 2: 双语模式 — 原文保留")
    print("=" * 60)

    t = make_translator(bilingual=True)
    html = b"<html><body><p>Hello world</p></body></html>"
    result = run_async(t.process_async(html, item_type=9)).decode()

    assert "Hello world" in result, "双语模式原文应保留"
    assert "你好世界" in result, "双语模式译文应出现"
    print(f"  结果片段(body内): {result[result.find('<body'):result.find('</body>')]}")
    print("  ✅ PASS: 双语模式原文保留")
    return True


def test_bilingual_original_class():
    print("\n" + "=" * 60)
    print("🧪 Test 3: 双语模式 — 原文含 epub-original class")
    print("=" * 60)

    t = make_translator(bilingual=True)
    html = b"<html><body><p>Hello world</p></body></html>"
    result = run_async(t.process_async(html, item_type=9)).decode()

    assert "epub-original" in result, "原文段落应含 epub-original class"
    print(f"  检测到 epub-original: True")
    print("  ✅ PASS: 原文标记 class 正确")
    return True


def test_bilingual_translated_class():
    print("\n" + "=" * 60)
    print("🧪 Test 4: 双语模式 — 译文含 epub-translated class")
    print("=" * 60)

    t = make_translator(bilingual=True)
    html = b"<html><body><p>Hello world</p></body></html>"
    result = run_async(t.process_async(html, item_type=9)).decode()

    assert "epub-translated" in result, "译文段落应含 epub-translated class"
    print(f"  检测到 epub-translated: True")
    print("  ✅ PASS: 译文标记 class 正确")
    return True


def test_untranslatable_paragraph_unchanged():
    print("\n" + "=" * 60)
    print("🧪 Test 5: 无需翻译的段落（纯数字）在双语模式下保持不变")
    print("=" * 60)

    t = make_translator(bilingual=True)
    html = b"<html><body><p>12345</p></body></html>"
    result = run_async(t.process_async(html, item_type=9)).decode()

    # 纯数字段落不应被翻译，不应有 epub-original/epub-translated
    assert "epub-original" not in result, "纯数字不应加 epub-original"
    assert "epub-translated" not in result, "纯数字不应加 epub-translated"
    print(f"  结果: {result[result.find('<p'):result.find('</p>')+4]}")
    print("  ✅ PASS: 无需翻译的段落保持原样")
    return True


def test_bilingual_paragraph_order():
    print("\n" + "=" * 60)
    print("🧪 Test 6: 双语模式 — 原文在前，译文在后")
    print("=" * 60)

    t = make_translator(bilingual=True)
    html = b"<html><body><p>Hello world</p></body></html>"
    result = run_async(t.process_async(html, item_type=9)).decode()

    orig_pos = result.find("epub-original")
    trans_pos = result.find("epub-translated")
    assert orig_pos < trans_pos, f"原文应在译文前（{orig_pos} vs {trans_pos}）"
    print(f"  epub-original 位置: {orig_pos}, epub-translated 位置: {trans_pos}")
    print("  ✅ PASS: 原文在前，译文在后")
    return True


# ─── 集成测试 ─────────────────────────────────────────────────────────────────

def test_compiler_passes_bilingual_flag():
    print("\n" + "=" * 60)
    print("🧪 Test 7: ExtremeCompiler bilingual 参数透传给 SemanticsTranslator")
    print("=" * 60)

    c = ExtremeCompiler(
        input_path="/tmp/dummy.epub",
        output_path="/tmp/dummy_out.epub",
        enable_translation=True,
        bilingual=True,
    )
    translator = next(
        (cl for cl in c.cleaners if isinstance(cl, SemanticsTranslator)), None
    )
    assert translator is not None, "SemanticsTranslator 应在 pipeline 中"
    assert translator.bilingual is True, f"bilingual 应为 True，实际为 {translator.bilingual}"
    print(f"  translator.bilingual = {translator.bilingual}")
    print("  ✅ PASS: bilingual 参数透传正确")
    return True


def test_compiler_bilingual_false_by_default():
    print("\n" + "=" * 60)
    print("🧪 Test 8: ExtremeCompiler bilingual 默认为 False")
    print("=" * 60)

    c = ExtremeCompiler(
        input_path="/tmp/dummy.epub",
        output_path="/tmp/dummy_out.epub",
        enable_translation=True,
    )
    translator = next(
        (cl for cl in c.cleaners if isinstance(cl, SemanticsTranslator)), None
    )
    assert translator is not None
    assert translator.bilingual is False, f"bilingual 默认应为 False"
    print("  ✅ PASS: bilingual 默认 False")
    return True


def test_job_model_has_bilingual_field():
    print("\n" + "=" * 60)
    print("🧪 Test 9: Job 模型含 bilingual 字段，默认 False")
    print("=" * 60)

    job = Job(
        id="test01",
        source_filename="book.epub",
        output_mode=OutputMode.simplified,
        trace_id="abc",
        input_path="/tmp/x.epub",
    )
    assert hasattr(job, "bilingual"), "Job 应有 bilingual 字段"
    assert job.bilingual is False, f"默认应为 False，实际为 {job.bilingual}"

    job2 = Job(
        id="test02",
        source_filename="book.epub",
        output_mode=OutputMode.simplified,
        trace_id="def",
        input_path="/tmp/x.epub",
        bilingual=True,
    )
    assert job2.bilingual is True
    print("  ✅ PASS: Job.bilingual 字段正确")
    return True


# ─── 主入口 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_default_mode_replaces_original,
        test_bilingual_keeps_original,
        test_bilingual_original_class,
        test_bilingual_translated_class,
        test_untranslatable_paragraph_unchanged,
        test_bilingual_paragraph_order,
        test_compiler_passes_bilingual_flag,
        test_compiler_bilingual_false_by_default,
        test_job_model_has_bilingual_field,
    ]

    passed = failed = 0
    for fn in tests:
        try:
            if fn():
                passed += 1
        except Exception as e:
            print(f"  ❌ FAIL: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"📊 C4 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
