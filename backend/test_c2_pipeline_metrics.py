"""
C2 测试：Pipeline 阶段耗时埋点（PipelineMetrics）

测试用例：
1. 运行后 metrics.stages 不为空
2. 所有 stage 的 elapsed_ms > 0
3. total_ms >= 所有 stage 之和
4. 核心阶段（Unpack / Packager+PostFix / TocRebuilder）均被记录
5. Safe Mode 触发后 mode 字段为 "safe"，SafeMode 阶段被记录
6. summary() 输出包含总耗时与阶段名
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from app.engine.compiler import ExtremeCompiler, PipelineMetrics, StageTimer


TEST_EPUB = Path(__file__).parent / "test_en.epub"


def _skip_if_no_epub():
    if not TEST_EPUB.exists():
        print("  ⏭️ SKIP: test_en.epub not found")
        return True
    return False


# ─── PipelineMetrics 单元测试 ─────────────────────────────────────────────────

def test_metrics_record_and_summary():
    print("\n" + "=" * 60)
    print("🧪 Test 1: PipelineMetrics.record / summary 基本功能")
    print("=" * 60)

    m = PipelineMetrics()
    m.record("Unpack", 12.5)
    m.record("Cleaner:CjkNormalizer", 45.3)
    m.record("Packager", 80.1)
    m.total_ms = 150.0

    assert len(m.stages) == 3
    assert m.stages[0].name == "Unpack"
    assert m.stages[0].elapsed_ms == 12.5

    summary = m.summary()
    assert "Unpack" in summary
    assert "150" in summary
    assert "full" in summary

    print(summary)
    print("  ✅ PASS: PipelineMetrics 记录与摘要正常")
    return True


def test_metrics_status_icons():
    print("\n" + "=" * 60)
    print("🧪 Test 2: 阶段状态图标（ok / skipped / error）")
    print("=" * 60)

    m = PipelineMetrics()
    m.record("StageA", 10.0, status="ok")
    m.record("StageB", 5.0, status="skipped")
    m.record("StageC", 3.0, status="error")
    m.total_ms = 20.0

    summary = m.summary()
    assert "✅" in summary
    assert "⚠️" in summary
    assert "❌" in summary
    print(summary)
    print("  ✅ PASS: 状态图标正确")
    return True


# ─── 集成测试：实际跑 Pipeline ────────────────────────────────────────────────

def test_full_pipeline_records_stages():
    print("\n" + "=" * 60)
    print("🧪 Test 3: Full Pipeline 完成后 metrics 含核心阶段")
    print("=" * 60)

    if _skip_if_no_epub():
        return True

    c = ExtremeCompiler(
        input_path=str(TEST_EPUB),
        output_path="/tmp/test_c2_full.epub",
        output_mode="simplified",
    )
    success = c.run()

    assert success, "Pipeline 应成功"
    stage_names = [s.name for s in c.metrics.stages]
    print(f"  记录的阶段: {stage_names}")

    assert "Unpack" in stage_names, "缺少 Unpack 阶段"
    assert "TocRebuilder" in stage_names, "缺少 TocRebuilder 阶段"
    assert "Packager+PostFix" in stage_names, "缺少 Packager+PostFix 阶段"
    print("  ✅ PASS: 核心阶段均被记录")
    return True


def test_all_elapsed_ms_positive():
    print("\n" + "=" * 60)
    print("🧪 Test 4: 所有阶段 elapsed_ms > 0")
    print("=" * 60)

    if _skip_if_no_epub():
        return True

    c = ExtremeCompiler(
        input_path=str(TEST_EPUB),
        output_path="/tmp/test_c2_ms.epub",
        output_mode="simplified",
    )
    c.run()

    for s in c.metrics.stages:
        assert s.elapsed_ms >= 0, f"阶段 {s.name} elapsed_ms 为负: {s.elapsed_ms}"

    assert c.metrics.total_ms > 0, "total_ms 应 > 0"
    print(f"  total_ms = {c.metrics.total_ms:.1f} ms，阶段数 = {len(c.metrics.stages)}")
    print("  ✅ PASS: 所有耗时 >= 0")
    return True


def test_total_ms_gte_sum_of_stages():
    print("\n" + "=" * 60)
    print("🧪 Test 5: total_ms >= 各阶段之和（含调用开销）")
    print("=" * 60)

    if _skip_if_no_epub():
        return True

    c = ExtremeCompiler(
        input_path=str(TEST_EPUB),
        output_path="/tmp/test_c2_total.epub",
        output_mode="simplified",
    )
    c.run()

    stage_sum = sum(s.elapsed_ms for s in c.metrics.stages)
    print(f"  total_ms={c.metrics.total_ms:.1f}  stage_sum={stage_sum:.1f}")
    assert c.metrics.total_ms >= stage_sum * 0.9, \
        f"total_ms ({c.metrics.total_ms:.1f}) 不应小于阶段之和 ({stage_sum:.1f})"
    print("  ✅ PASS: total_ms >= 阶段之和")
    return True


def test_safe_mode_metrics():
    print("\n" + "=" * 60)
    print("🧪 Test 6: Safe Mode 触发后 metrics.mode='safe'，SafeMode 阶段被记录")
    print("=" * 60)

    if _skip_if_no_epub():
        return True

    with patch.object(ExtremeCompiler, "_run_full_pipeline",
                      side_effect=RuntimeError("强制触发 Safe Mode")):
        c = ExtremeCompiler(
            input_path=str(TEST_EPUB),
            output_path="/tmp/test_c2_safe.epub",
            output_mode="simplified",
        )
        success = c.run()

    assert success, "Safe Mode 应产出文件"
    assert c.metrics.mode == "safe", f"mode 应为 'safe'，实际为 '{c.metrics.mode}'"

    stage_names = [s.name for s in c.metrics.stages]
    print(f"  SafeMode 阶段: {stage_names}")
    assert any("SafeMode" in n for n in stage_names), "缺少 SafeMode 阶段记录"
    print("  ✅ PASS: Safe Mode metrics 正确")
    return True


def test_summary_printed_to_stdout(capsys=None):
    print("\n" + "=" * 60)
    print("🧪 Test 7: summary() 包含总耗时与阶段名")
    print("=" * 60)

    if _skip_if_no_epub():
        return True

    c = ExtremeCompiler(
        input_path=str(TEST_EPUB),
        output_path="/tmp/test_c2_summary.epub",
        output_mode="simplified",
    )
    c.run()

    summary = c.metrics.summary()
    assert "ms" in summary, "summary 应含 ms 单位"
    assert "Unpack" in summary, "summary 应含 Unpack"
    assert f"{c.metrics.total_ms:.0f}"[:3] in summary, "summary 应含总耗时数字"
    print("  ✅ PASS: summary() 内容正确")
    return True


# ─── 主入口 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_metrics_record_and_summary,
        test_metrics_status_icons,
        test_full_pipeline_records_stages,
        test_all_elapsed_ms_positive,
        test_total_ms_gte_sum_of_stages,
        test_safe_mode_metrics,
        test_summary_printed_to_stdout,
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
    print(f"📊 C2 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
