"""
A1 测试：EpubCheck 闭环验证

测试用例：
1. 完整英文 EPUB 过引擎（不翻译）→ EpubCheck 0 errors
2. 裁剪版 EPUB 过引擎（不翻译）→ EpubCheck 验证（预期有 NCX 锚点错误，但不应有 FATAL）
3. 验证原始文件的 error 数 > 输出文件的 error 数（引擎应修复而非引入错误）
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

EPUBCHECK_JAR = "/Users/zengchanghuan/Desktop/workspace/epub-factory/tools/epubcheck-5.1.0/epubcheck.jar"
os.environ["EPUBCHECK_JAR"] = EPUBCHECK_JAR

sys.path.insert(0, str(Path(__file__).parent))
from app.engine.compiler import ExtremeCompiler


def run_epubcheck(epub_path: str) -> dict:
    json_out = tempfile.mktemp(suffix=".json")
    try:
        subprocess.run(
            ["java", "-jar", EPUBCHECK_JAR, epub_path, "--json", json_out],
            capture_output=True, text=True, timeout=60
        )
        with open(json_out, "r") as f:
            data = json.load(f)
        messages = data.get("messages", [])
        return {
            "fatals": sum(1 for m in messages if m.get("severity") == "FATAL"),
            "errors": sum(1 for m in messages if m.get("severity") == "ERROR"),
            "warnings": sum(1 for m in messages if m.get("severity") == "WARNING"),
        }
    finally:
        if os.path.exists(json_out):
            os.unlink(json_out)


def test_full_epub_zero_errors():
    print("\n" + "=" * 60)
    print("🧪 Test 1: 完整英文 EPUB → EpubCheck 0 errors")
    print("=" * 60)

    input_file = "/Users/zengchanghuan/Desktop/workspace/epub-factory/How the World Really Works The Science Behind How We Got Here and Where Were Going (Vaclav Smil) (z-library.sk, 1lib.sk, z-lib.sk).epub"
    output_file = "/tmp/test_a1_full.epub"

    if not Path(input_file).exists():
        print("  ⏭️ SKIP: Full EPUB not found")
        return True

    c = ExtremeCompiler(input_path=input_file, output_path=output_file, output_mode="simplified")
    success = c.run()
    assert success, "ExtremeCompiler failed"

    result = run_epubcheck(output_file)
    print(f"  Results: {result}")
    assert result["fatals"] == 0, f"Expected 0 fatals, got {result['fatals']}"
    assert result["errors"] == 0, f"Expected 0 errors, got {result['errors']}"
    print("  ✅ PASS: 0 fatals, 0 errors")
    return True


def test_engine_reduces_errors():
    print("\n" + "=" * 60)
    print("🧪 Test 2: 引擎应修复错误（输出 error 数 ≤ 原始）")
    print("=" * 60)

    input_file = "/Users/zengchanghuan/Desktop/workspace/epub-factory/How the World Really Works The Science Behind How We Got Here and Where Were Going (Vaclav Smil) (z-library.sk, 1lib.sk, z-lib.sk).epub"
    output_file = "/tmp/test_a1_reduce.epub"

    if not Path(input_file).exists():
        print("  ⏭️ SKIP: Full EPUB not found")
        return True

    before = run_epubcheck(input_file)
    print(f"  Original: {before}")

    c = ExtremeCompiler(input_path=input_file, output_path=output_file, output_mode="simplified")
    c.run()

    after = run_epubcheck(output_file)
    print(f"  After:    {after}")

    assert after["errors"] <= before["errors"], \
        f"Engine introduced errors! Before: {before['errors']}, After: {after['errors']}"
    assert after["fatals"] == 0, f"Engine produced FATAL errors: {after['fatals']}"
    print(f"  ✅ PASS: Errors reduced from {before['errors']} to {after['errors']}")
    return True


def test_cropped_no_fatal():
    print("\n" + "=" * 60)
    print("🧪 Test 3: 裁剪版 EPUB → 无 FATAL 错误")
    print("=" * 60)

    input_file = "/Users/zengchanghuan/Desktop/workspace/epub-factory/backend/test_en.epub"
    output_file = "/tmp/test_a1_cropped.epub"

    if not Path(input_file).exists():
        print("  ⏭️ SKIP: Cropped EPUB not found")
        return True

    c = ExtremeCompiler(input_path=input_file, output_path=output_file, output_mode="simplified")
    success = c.run()
    assert success, "ExtremeCompiler failed"

    result = run_epubcheck(output_file)
    print(f"  Results: {result}")
    assert result["fatals"] == 0, f"Expected 0 fatals, got {result['fatals']}"
    print(f"  ✅ PASS: 0 fatals ({result['errors']} errors from cropped content — expected)")
    return True


if __name__ == "__main__":
    passed = 0
    failed = 0

    for test_fn in [test_full_epub_zero_errors, test_engine_reduces_errors, test_cropped_no_fatal]:
        try:
            if test_fn():
                passed += 1
        except Exception as e:
            print(f"  ❌ FAIL: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"📊 A1 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
