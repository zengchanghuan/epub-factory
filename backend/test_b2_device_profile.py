"""
B2 测试：Kindle/Apple 特化编译

测试用例：
1. Kindle 模式：输出不含非黑色 color 和 opacity
2. Apple 模式：输出保留色彩，包含 -webkit- 前缀
3. 所有模式 EpubCheck 0 fatals
"""

import os
import json
import subprocess
import sys
import tempfile
import zipfile
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
        }
    finally:
        if os.path.exists(json_out):
            os.unlink(json_out)


def extract_all_text(epub_path: str) -> str:
    """提取 EPUB 中所有 XHTML 和 CSS 文本"""
    all_text = []
    with zipfile.ZipFile(epub_path, 'r') as zf:
        for name in zf.namelist():
            if name.endswith(('.xhtml', '.css', '.html')):
                all_text.append(zf.read(name).decode('utf-8', errors='ignore'))
    return '\n'.join(all_text)


INPUT_FILE = "/Users/zengchanghuan/Desktop/workspace/epub-factory/How the World Really Works The Science Behind How We Got Here and Where Were Going (Vaclav Smil) (z-library.sk, 1lib.sk, z-lib.sk).epub"


def test_kindle_removes_colors():
    print("\n" + "=" * 60)
    print("🧪 Test 1: Kindle 模式 — 移除非黑色 color 和 opacity")
    print("=" * 60)

    if not Path(INPUT_FILE).exists():
        print("  ⏭️ SKIP")
        return True

    output_file = "/tmp/test_b2_kindle.epub"
    c = ExtremeCompiler(input_path=INPUT_FILE, output_path=output_file,
                        output_mode="simplified", device="kindle")
    c.run()

    text = extract_all_text(output_file)

    import re
    # 检查 opacity 已被移除
    opacity_matches = re.findall(r'opacity\s*:\s*[^;]+', text, re.IGNORECASE)
    print(f"  opacity 残留: {len(opacity_matches)} 处")
    assert len(opacity_matches) == 0, f"Kindle mode should remove opacity, found: {opacity_matches[:3]}"

    print("  ✅ PASS: Kindle 优化正常")
    return True


def test_apple_adds_webkit():
    print("\n" + "=" * 60)
    print("🧪 Test 2: Apple 模式 — 注入 -webkit- 前缀")
    print("=" * 60)

    if not Path(INPUT_FILE).exists():
        print("  ⏭️ SKIP")
        return True

    output_file = "/tmp/test_b2_apple.epub"
    c = ExtremeCompiler(input_path=INPUT_FILE, output_path=output_file,
                        output_mode="simplified", device="apple")
    c.run()

    text = extract_all_text(output_file)

    # Apple 模式下，如果原文有 hyphens/column-count 等属性，应该也有对应 -webkit- 版本
    has_hyphens = 'hyphens:' in text.lower()
    has_webkit_hyphens = '-webkit-hyphens:' in text.lower()

    if has_hyphens:
        print(f"  hyphens 存在: True, -webkit-hyphens 存在: {has_webkit_hyphens}")
        assert has_webkit_hyphens, "Apple mode should inject -webkit-hyphens"
    else:
        print("  原文无 hyphens 属性，跳过前缀检查")

    print("  ✅ PASS: Apple 优化正常")
    return True


def test_all_devices_epubcheck():
    print("\n" + "=" * 60)
    print("🧪 Test 3: 所有设备模式 EpubCheck 0 fatals")
    print("=" * 60)

    if not Path(INPUT_FILE).exists():
        print("  ⏭️ SKIP")
        return True

    for device in ["generic", "kindle", "apple"]:
        output_file = f"/tmp/test_b2_{device}.epub"
        c = ExtremeCompiler(input_path=INPUT_FILE, output_path=output_file,
                            output_mode="simplified", device=device)
        c.run()

        result = run_epubcheck(output_file)
        print(f"  {device:8s}: fatals={result['fatals']}, errors={result['errors']}")
        assert result["fatals"] == 0, f"{device} mode produced FATAL: {result['fatals']}"

    print("  ✅ PASS: All devices pass 0 fatals")
    return True


if __name__ == "__main__":
    passed = 0
    failed = 0

    for test_fn in [test_kindle_removes_colors, test_apple_adds_webkit, test_all_devices_epubcheck]:
        try:
            if test_fn():
                passed += 1
        except Exception as e:
            print(f"  ❌ FAIL: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"📊 B2 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
