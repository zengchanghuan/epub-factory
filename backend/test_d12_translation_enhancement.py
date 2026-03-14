"""
D12 测试：翻译链路增强

- _looks_like_html / _looks_like_error_response 结果校验
- _candidate_routes 主/备 base_url、model 组合
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.engine.cleaners.semantics_translator import SemanticsTranslator


def test_looks_like_html():
    assert SemanticsTranslator._looks_like_html("<p>Hello</p>") is True
    assert SemanticsTranslator._looks_like_html("<div>内容</div>") is True
    assert SemanticsTranslator._looks_like_html("") is False
    assert SemanticsTranslator._looks_like_html("   \n  ") is False
    assert SemanticsTranslator._looks_like_html("no tags here") is False


def test_looks_like_error_response():
    assert SemanticsTranslator._looks_like_error_response("Error: rate limit") is True
    assert SemanticsTranslator._looks_like_error_response("Sorry, I cannot translate.") is True
    assert SemanticsTranslator._looks_like_error_response("I'm unable to process") is True
    assert SemanticsTranslator._looks_like_error_response("<p>Normal translation</p>") is False
    assert SemanticsTranslator._looks_like_error_response("") is False


def test_candidate_routes_default():
    """无 fallback 时仅返回主 base_url + 主 model。"""
    t = SemanticsTranslator(target_lang="zh-CN")
    routes = t._candidate_routes()
    assert len(routes) >= 1
    assert routes[0] == (t.base_url, t.model)


def test_candidate_routes_with_fallbacks():
    """有 OPENAI_BASE_URL_FALLBACKS / OPENAI_MODEL_FALLBACKS 时路由含备选。"""
    os.environ["OPENAI_BASE_URL_FALLBACKS"] = "https://fallback1.com/v1,https://fallback2.com/v1"
    os.environ["OPENAI_MODEL_FALLBACKS"] = "gpt-4o"
    try:
        t = SemanticsTranslator(target_lang="zh-CN")
        routes = t._candidate_routes()
        assert len(routes) >= 2
        bases = {r[0] for r in routes}
        models = {r[1] for r in routes}
        assert t.base_url in bases
        assert "https://fallback1.com/v1" in bases or "https://fallback2.com/v1" in bases
        assert t.model in models
        assert "gpt-4o" in models
    finally:
        os.environ.pop("OPENAI_BASE_URL_FALLBACKS", None)
        os.environ.pop("OPENAI_MODEL_FALLBACKS", None)


def _run():
    cases = [
        test_looks_like_html,
        test_looks_like_error_response,
        test_candidate_routes_default,
        test_candidate_routes_with_fallbacks,
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
