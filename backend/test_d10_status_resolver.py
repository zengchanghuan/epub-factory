"""
D10 测试：EpubCheck 与状态判定

- resolve_after_conversion：校验未通过 -> failed + EPUB_VALIDATION_FAILED
- resolve_after_conversion：校验通过 -> success（保留 PARTIAL_TRANSLATION 等 error_code）
- 边界：无 validation_passed 属性时视为 True（向后兼容）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.models import ConversionResult, JobStatus
from app.domain.status_resolver import resolve_after_conversion, EPUB_VALIDATION_FAILED


def test_resolve_validation_failed_returns_failed():
    r = ConversionResult(message="转换成功", validation_passed=False)
    status, msg, code = resolve_after_conversion(r)
    assert status == JobStatus.failed
    assert code == EPUB_VALIDATION_FAILED


def test_resolve_validation_passed_returns_success():
    r = ConversionResult(message="转换成功", validation_passed=True)
    status, msg, code = resolve_after_conversion(r)
    assert status == JobStatus.success
    assert code is None


def test_resolve_partial_translation_stays_success():
    r = ConversionResult(
        message="转换成功，但有 2 个段落翻译失败",
        error_code="PARTIAL_TRANSLATION",
        validation_passed=True,
    )
    status, msg, code = resolve_after_conversion(r)
    assert status == JobStatus.success
    assert code == "PARTIAL_TRANSLATION"


def test_resolve_missing_validation_passed_defaults_true():
    """无 validation_passed 时视为 True，避免旧 ConversionResult 被误判为失败"""
    class MockResult:
        message = "OK"
        error_code = None
    r = MockResult()
    assert not hasattr(r, "validation_passed")
    status, _, _ = resolve_after_conversion(r)
    assert status == JobStatus.success


def _run():
    cases = [
        test_resolve_validation_failed_returns_failed,
        test_resolve_validation_passed_returns_success,
        test_resolve_partial_translation_stays_success,
        test_resolve_missing_validation_passed_defaults_true,
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
