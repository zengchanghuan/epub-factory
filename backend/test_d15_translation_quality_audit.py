"""
D15 测试：规则型翻译可信度审计。

覆盖不增加 LLM 成本的关键质量信号：
- 长度异常
- 数字丢失
- HTML 标签破坏
- 模型错误/拒答响应
- glossary 译名缺失
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.domain.translation_quality_audit import audit_translation_chunk


def test_audit_ok_translation():
    audit = audit_translation_chunk(
        original_html="<p>Mr. Smith met 42 readers in London.</p>",
        translated_html="史密斯先生在伦敦见到了 42 位读者。",
        glossary={"Smith": "史密斯", "London": "伦敦"},
    )
    assert audit.risk_level == "ok"
    assert audit.flags == []
    assert audit.numbers_missing == []
    assert audit.latin_terms_missing == []


def test_audit_suspiciously_short_translation():
    audit = audit_translation_chunk(
        original_html="<p>This is a long paragraph about books, politics, publishers, translators, and censorship.</p>",
        translated_html="书。",
        glossary={},
    )
    assert audit.risk_level == "warn"
    assert "suspiciously_short_translation" in audit.flags


def test_audit_numbers_missing():
    audit = audit_translation_chunk(
        original_html="<p>The study covered 128 cases in 2024.</p>",
        translated_html="这项研究覆盖了多个案例。",
        glossary={},
    )
    assert audit.risk_level == "warn"
    assert "numbers_missing" in audit.flags
    assert "128" in audit.numbers_missing
    assert "2024" in audit.numbers_missing


def test_audit_html_tag_mismatch_is_fail():
    audit = audit_translation_chunk(
        original_html="<p>This is <em>important</em>.</p>",
        translated_html="这是重要的。",
        glossary={},
    )
    assert audit.risk_level == "fail"
    assert audit.html_tag_mismatch is True
    assert "html_tag_mismatch" in audit.flags


def test_audit_error_like_response_is_fail():
    audit = audit_translation_chunk(
        original_html="<p>Translate this.</p>",
        translated_html="Sorry, I cannot translate this content.",
        glossary={},
        error_like_checker=lambda s: "cannot translate" in s.lower(),
    )
    assert audit.risk_level == "fail"
    assert audit.error_like_response is True
    assert "error_like_response" in audit.flags


def test_audit_glossary_terms_missing():
    audit = audit_translation_chunk(
        original_html="<p>Smith traveled to London.</p>",
        translated_html="他去了那里。",
        glossary={"Smith": "史密斯", "London": "伦敦"},
    )
    assert audit.risk_level == "warn"
    assert "glossary_terms_missing" in audit.flags
    assert audit.latin_terms_missing == ["Smith", "London"]


def test_audit_likely_untranslated_english_is_fail():
    audit = audit_translation_chunk(
        original_html="<p>This account of the events which led to the solution of DNA is unique in several ways.</p>",
        translated_html="This account of the events which led to the solution of DNA is unique in several ways.",
        glossary={},
    )
    assert audit.risk_level == "fail"
    assert audit.likely_untranslated is True
    assert "likely_untranslated" in audit.flags


def _run():
    cases = [
        test_audit_ok_translation,
        test_audit_suspiciously_short_translation,
        test_audit_numbers_missing,
        test_audit_html_tag_mismatch_is_fail,
        test_audit_error_like_response_is_fail,
        test_audit_glossary_terms_missing,
        test_audit_likely_untranslated_english_is_fail,
    ]
    passed = 0
    for fn in cases:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  ❌ {fn.__name__}: {exc}")
            raise
    print(f"\n📊 {passed} passed, 0 failed")


if __name__ == "__main__":
    _run()
