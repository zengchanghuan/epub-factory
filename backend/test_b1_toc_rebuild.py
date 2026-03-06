"""
B1 测试：启发式目录重建（TOC Rebuild）

测试用例：
1. 完整 EPUB 过引擎 → TOC 条目数 > 0
2. EpubCheck 0 fatals（允许极少量非关键 errors）
3. TOC 条目数合理（不会误抓太多）
"""

import os
import json
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


def test_toc_entries_generated():
    print("\n" + "=" * 60)
    print("🧪 Test 1: TOC 条目数 > 0 且合理")
    print("=" * 60)

    input_file = "/Users/zengchanghuan/Desktop/workspace/epub-factory/How the World Really Works The Science Behind How We Got Here and Where Were Going (Vaclav Smil) (z-library.sk, 1lib.sk, z-lib.sk).epub"
    output_file = "/tmp/test_b1_toc.epub"

    if not Path(input_file).exists():
        print("  ⏭️ SKIP: Full EPUB not found")
        return True

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        c = ExtremeCompiler(input_path=input_file, output_path=output_file, output_mode="simplified")
        c.run()

    output = buf.getvalue()
    print(f"  Engine output (excerpt): {output[:300]}")

    # 从输出中提取 TOC 条目数
    import re
    match = re.search(r'\[TOC\] Rebuilt with (\d+) entries', output)
    assert match, "TOC rebuild message not found in output"

    count = int(match.group(1))
    print(f"  TOC entries: {count}")
    assert count > 5, f"Expected > 5 entries, got {count}"
    assert count < 500, f"Too many entries ({count}), likely false positives"
    print(f"  ✅ PASS: {count} TOC entries (reasonable)")
    return True


def test_toc_no_fatal():
    print("\n" + "=" * 60)
    print("🧪 Test 2: EpubCheck 0 fatals（TOC 重建后）")
    print("=" * 60)

    output_file = "/tmp/test_b1_toc.epub"
    if not Path(output_file).exists():
        print("  ⏭️ SKIP: Output not found (Test 1 may have been skipped)")
        return True

    result = run_epubcheck(output_file)
    print(f"  Results: {result}")
    assert result["fatals"] == 0, f"Expected 0 fatals, got {result['fatals']}"
    # 允许极少量边缘 case 的 errors（如 br 标签导致的锚点失配）
    assert result["errors"] <= 2, f"Too many errors: {result['errors']}"
    print(f"  ✅ PASS: 0 fatals, {result['errors']} minor errors (acceptable)")
    return True


def test_toc_improvement():
    print("\n" + "=" * 60)
    print("🧪 Test 3: 引擎不引入新的 FATAL 或大量 errors")
    print("=" * 60)

    input_file = "/Users/zengchanghuan/Desktop/workspace/epub-factory/How the World Really Works The Science Behind How We Got Here and Where Were Going (Vaclav Smil) (z-library.sk, 1lib.sk, z-lib.sk).epub"
    output_file = "/tmp/test_b1_toc.epub"

    if not Path(input_file).exists() or not Path(output_file).exists():
        print("  ⏭️ SKIP")
        return True

    before = run_epubcheck(input_file)
    after = run_epubcheck(output_file)

    print(f"  Before: {before}")
    print(f"  After:  {after}")

    # errors 应该大致持平或减少（不因 TOC 重建而大量增加）
    assert after["errors"] <= before["errors"] + 2, \
        f"Engine introduced too many errors: {before['errors']} -> {after['errors']}"
    print(f"  ✅ PASS: Error count stable ({before['errors']} -> {after['errors']})")
    return True


if __name__ == "__main__":
    passed = 0
    failed = 0

    for test_fn in [test_toc_entries_generated, test_toc_no_fatal, test_toc_improvement]:
        try:
            if test_fn():
                passed += 1
        except Exception as e:
            print(f"  ❌ FAIL: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"📊 B1 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
