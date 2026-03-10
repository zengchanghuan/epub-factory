"""
C1 测试：排版增强器（TypographyEnhancer）与引擎降级策略

测试用例：
1. CSS 注入 orphans/widows/overflow-wrap
2. CSS 不重复注入（幂等性）
3. HTML 文本节点中 ... → … 替换
4. HTML 文本节点中 -- → — 替换
5. HTML 标签属性内的内容不被误修改
6. 降级策略：完整 Pipeline 失败时输出 Safe Mode 文件
7. 清洗器层异常隔离：单个 Cleaner 抛异常不影响整体任务
"""

import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from app.engine.cleaners.typography_enhancer import TypographyEnhancer
from app.engine.compiler import ExtremeCompiler


# ─── TypographyEnhancer 单元测试 ─────────────────────────────────────────────

def test_css_injects_orphans_widows():
    print("\n" + "=" * 60)
    print("🧪 Test 1: CSS 注入 orphans / widows / overflow-wrap")
    print("=" * 60)

    te = TypographyEnhancer()
    result = te.process(b"p { color: red; }", item_type=2).decode()

    assert "orphans" in result, "orphans 未注入"
    assert "widows" in result, "widows 未注入"
    assert "overflow-wrap" in result, "overflow-wrap 未注入"
    print("  ✅ PASS: CSS 排版规则已注入")
    return True


def test_css_idempotent():
    print("\n" + "=" * 60)
    print("🧪 Test 2: CSS 幂等性 — 不重复注入")
    print("=" * 60)

    te = TypographyEnhancer()
    first = te.process(b"p { color: red; }", item_type=2).decode()
    second = te.process(b"body { font-size: 1em; }", item_type=2).decode()

    assert first.count("orphans") == 1, "第一次注入应只有一个 orphans"
    assert "orphans" not in second, "同一 Enhancer 实例不应对第二个 CSS 文件重复注入"
    print("  ✅ PASS: CSS 注入幂等，不重复")
    return True


def test_ellipsis_replacement():
    print("\n" + "=" * 60)
    print("🧪 Test 3: HTML 文本节点 ... → …")
    print("=" * 60)

    te = TypographyEnhancer()
    result = te.process(b"<p>Wait...</p>", item_type=9).decode()

    assert "…" in result, "省略号未被替换"
    assert "..." not in result, "原始三点未被清除"
    print(f"  输出: {result.strip()}")
    print("  ✅ PASS: 省略号已替换")
    return True


def test_dash_replacement():
    print("\n" + "=" * 60)
    print("🧪 Test 4: HTML 文本节点 -- → —")
    print("=" * 60)

    te = TypographyEnhancer()
    result = te.process(b"<p>Note -- see below</p>", item_type=9).decode()

    assert "\u2014" in result, "破折号未被替换"
    assert " -- " not in result, "原始双短横未被清除"
    print(f"  输出: {result.strip()}")
    print("  ✅ PASS: 破折号已替换")
    return True


def test_html_attributes_untouched():
    print("\n" + "=" * 60)
    print("🧪 Test 5: HTML 标签属性内的内容不被误修改")
    print("=" * 60)

    te = TypographyEnhancer()
    # href 中的 -- 不应被替换
    html = b'<a href="http://example.com/path--end">click...</a>'
    result = te.process(html, item_type=9).decode()

    assert "http://example.com/path--end" in result, \
        "href 属性中的 -- 被错误替换"
    # 文本节点中的 ... 应该被替换
    assert "click…" in result, "文本节点中的省略号应被替换"
    print(f"  输出: {result.strip()}")
    print("  ✅ PASS: 属性内容安全，文本节点正确替换")
    return True


def test_non_html_css_passthrough():
    print("\n" + "=" * 60)
    print("🧪 Test 6: 非 HTML/CSS 内容原样透传")
    print("=" * 60)

    te = TypographyEnhancer()
    raw = b"\x89PNG\r\n"
    result = te.process(raw, item_type=3)  # item_type=3 是图片

    assert result == raw, "非 HTML/CSS 内容应原样返回"
    print("  ✅ PASS: 非目标类型内容原样透传")
    return True


# ─── 降级策略集成测试 ──────────────────────────────────────────────────────────

def test_safe_mode_on_pipeline_failure():
    print("\n" + "=" * 60)
    print("🧪 Test 7: 降级策略 — Full Pipeline 失败后启用 Safe Mode")
    print("=" * 60)

    test_epub = Path(__file__).parent / "test_en.epub"
    if not test_epub.exists():
        print("  ⏭️ SKIP: test_en.epub not found")
        return True

    output = "/tmp/test_c1_safemode.epub"

    # mock EpubPackager.save 在第一次调用时抛异常（模拟 Full Pipeline 失败）
    original_run_full = ExtremeCompiler._run_full_pipeline
    call_count = {"n": 0}

    def fake_full_pipeline(self):
        call_count["n"] += 1
        raise RuntimeError("模拟 Full Pipeline 崩溃")

    with patch.object(ExtremeCompiler, "_run_full_pipeline", fake_full_pipeline):
        c = ExtremeCompiler(
            input_path=str(test_epub),
            output_path=output,
            output_mode="simplified",
        )
        success = c.run()

    assert success, "Safe Mode 应成功产出文件"
    assert Path(output).exists(), "Safe Mode 输出文件不存在"

    # 验证输出是合法 EPUB（能被 zipfile 打开）
    with zipfile.ZipFile(output, "r") as zf:
        names = zf.namelist()
    assert "mimetype" in names, "Safe Mode 输出应含 mimetype"

    print(f"  Full Pipeline 触发异常次数: {call_count['n']}")
    print(f"  Safe Mode 输出文件: {output} ({Path(output).stat().st_size} bytes)")
    print("  ✅ PASS: Safe Mode 降级成功")
    return True


def test_cleaner_isolation():
    print("\n" + "=" * 60)
    print("🧪 Test 8: 清洗器异常隔离 — 单个 Cleaner 崩溃不影响整体")
    print("=" * 60)

    test_epub = Path(__file__).parent / "test_en.epub"
    if not test_epub.exists():
        print("  ⏭️ SKIP: test_en.epub not found")
        return True

    output = "/tmp/test_c1_isolation.epub"

    # 让 CssSanitizer.process 每次调用都抛异常
    from app.engine.cleaners.css_sanitizer import CssSanitizer
    with patch.object(CssSanitizer, "process", side_effect=RuntimeError("模拟 CSS 清洗崩溃")):
        c = ExtremeCompiler(
            input_path=str(test_epub),
            output_path=output,
            output_mode="simplified",
        )
        success = c.run()

    assert success, "单个 Cleaner 崩溃后整体任务应仍然成功"
    assert Path(output).exists(), "输出文件应存在"

    with zipfile.ZipFile(output, "r") as zf:
        names = zf.namelist()
    assert "mimetype" in names, "输出应为合法 EPUB"

    print(f"  输出文件: {output} ({Path(output).stat().st_size} bytes)")
    print("  ✅ PASS: 清洗器异常被隔离，整体任务完成")
    return True


# ─── 主入口 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_css_injects_orphans_widows,
        test_css_idempotent,
        test_ellipsis_replacement,
        test_dash_replacement,
        test_html_attributes_untouched,
        test_non_html_css_passthrough,
        test_safe_mode_on_pipeline_failure,
        test_cleaner_isolation,
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
    print(f"📊 C1 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
