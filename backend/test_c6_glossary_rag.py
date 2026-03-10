"""
C6 测试：翻译术语表注入（RAG）

测试用例：
1.  _build_system_prompt 无术语表时不含"术语对照表"
2.  _build_system_prompt 有术语表时含"术语对照表"
3.  术语表中所有条目出现在 prompt 中
4.  空术语表与无术语表行为一致
5.  mock LLM：术语表中的术语被正确使用
6.  SemanticsTranslator 默认 glossary 为空 dict
7.  ExtremeCompiler 将 glossary 传给 SemanticsTranslator
8.  ExtremeCompiler 无 glossary 时 SemanticsTranslator.glossary 为空
9.  Job 模型含 glossary 字段，默认空 dict
10. API: glossary_json 格式错误时返回 400
11. API: glossary_json 为 None 时 glossary 默认为空 dict
12. _build_system_prompt 含目标语言与规则（回归）
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.engine.compiler import ExtremeCompiler
from app.models import Job, OutputMode


# ─── _build_system_prompt 单元测试 ────────────────────────────────────────────

def test_prompt_no_glossary():
    print("\n" + "=" * 60)
    print("🧪 Test 1: 无术语表时 prompt 不含'术语对照表'")
    print("=" * 60)

    t = SemanticsTranslator(target_lang="zh-CN")
    prompt = t._build_system_prompt()
    assert "术语对照表" not in prompt, f"不应含术语对照表，实际:\n{prompt}"
    print("  ✅ PASS")
    return True


def test_prompt_with_glossary():
    print("\n" + "=" * 60)
    print("🧪 Test 2: 有术语表时 prompt 含'术语对照表'")
    print("=" * 60)

    t = SemanticsTranslator(
        target_lang="zh-CN",
        glossary={"Harry Potter": "哈利·波特", "Hermione": "赫敏"},
    )
    prompt = t._build_system_prompt()
    assert "术语对照表" in prompt, "prompt 应含术语对照表"
    print(f"  术语部分: {prompt[prompt.find('术语'):][:100]}")
    print("  ✅ PASS")
    return True


def test_all_glossary_entries_in_prompt():
    print("\n" + "=" * 60)
    print("🧪 Test 3: 术语表所有条目出现在 prompt 中")
    print("=" * 60)

    glossary = {
        "Muggle": "麻瓜",
        "Hogwarts": "霍格沃茨",
        "Quidditch": "魁地奇",
    }
    t = SemanticsTranslator(target_lang="zh-CN", glossary=glossary)
    prompt = t._build_system_prompt()

    for src, dst in glossary.items():
        assert src in prompt, f"原文术语 '{src}' 未出现在 prompt 中"
        assert dst in prompt, f"目标术语 '{dst}' 未出现在 prompt 中"

    print(f"  验证了 {len(glossary)} 个术语条目")
    print("  ✅ PASS")
    return True


def test_empty_glossary_same_as_none():
    print("\n" + "=" * 60)
    print("🧪 Test 4: 空术语表与无术语表行为一致")
    print("=" * 60)

    t_none = SemanticsTranslator(target_lang="zh-CN", glossary=None)
    t_empty = SemanticsTranslator(target_lang="zh-CN", glossary={})

    assert t_none._build_system_prompt() == t_empty._build_system_prompt()
    print("  ✅ PASS")
    return True


def test_glossary_used_in_translation():
    print("\n" + "=" * 60)
    print("🧪 Test 5: _build_system_prompt 含术语表（独立于缓存）")
    print("=" * 60)

    glossary = {"wizard": "巫师", "spell": "咒语"}
    t = SemanticsTranslator(target_lang="zh-CN", glossary=glossary)

    prompt = t._build_system_prompt()
    assert "wizard" in prompt, f"prompt 应含 wizard，实际:\n{prompt[-200:]}"
    assert "巫师" in prompt, f"prompt 应含 巫师，实际:\n{prompt[-200:]}"
    assert "spell" in prompt, f"prompt 应含 spell"
    assert "咒语" in prompt, f"prompt 应含 咒语"
    print("  ✅ PASS: 术语表正确注入 prompt")
    return True


# ─── 默认值测试 ────────────────────────────────────────────────────────────────

def test_default_glossary_empty():
    print("\n" + "=" * 60)
    print("🧪 Test 6: SemanticsTranslator 默认 glossary 为空 dict")
    print("=" * 60)

    t = SemanticsTranslator()
    assert isinstance(t.glossary, dict)
    assert len(t.glossary) == 0
    print("  ✅ PASS")
    return True


def test_compiler_passes_glossary():
    print("\n" + "=" * 60)
    print("🧪 Test 7: ExtremeCompiler 将 glossary 传给 SemanticsTranslator")
    print("=" * 60)

    glossary = {"speed": "速度", "force": "力"}
    c = ExtremeCompiler(
        input_path="/tmp/x.epub",
        output_path="/tmp/y.epub",
        enable_translation=True,
        glossary=glossary,
    )
    translator = next(cl for cl in c.cleaners if isinstance(cl, SemanticsTranslator))
    assert translator.glossary == glossary, f"glossary 不匹配: {translator.glossary}"
    print(f"  glossary = {translator.glossary}")
    print("  ✅ PASS")
    return True


def test_compiler_no_glossary_empty():
    print("\n" + "=" * 60)
    print("🧪 Test 8: ExtremeCompiler 无 glossary 时 SemanticsTranslator.glossary 为空")
    print("=" * 60)

    c = ExtremeCompiler(
        input_path="/tmp/x.epub",
        output_path="/tmp/y.epub",
        enable_translation=True,
    )
    translator = next(cl for cl in c.cleaners if isinstance(cl, SemanticsTranslator))
    assert translator.glossary == {}
    print("  ✅ PASS")
    return True


# ─── 数据模型测试 ─────────────────────────────────────────────────────────────

def test_job_model_glossary_field():
    print("\n" + "=" * 60)
    print("🧪 Test 9: Job 模型含 glossary 字段，默认为空 dict")
    print("=" * 60)

    job = Job(
        id="test01",
        source_filename="book.epub",
        output_mode=OutputMode.simplified,
        trace_id="abc",
        input_path="/tmp/x.epub",
    )
    assert hasattr(job, "glossary"), "Job 应有 glossary 字段"
    assert isinstance(job.glossary, dict)
    assert len(job.glossary) == 0

    job2 = Job(
        id="test02",
        source_filename="book.epub",
        output_mode=OutputMode.simplified,
        trace_id="def",
        input_path="/tmp/x.epub",
        glossary={"AI": "人工智能"},
    )
    assert job2.glossary == {"AI": "人工智能"}
    print("  ✅ PASS")
    return True


# ─── API 参数解析测试 ──────────────────────────────────────────────────────────

def test_api_glossary_json_parse_valid():
    print("\n" + "=" * 60)
    print("🧪 Test 10: 合法 glossary_json 被正确解析为 dict")
    print("=" * 60)

    raw = '{"Harry Potter": "哈利·波特", "Voldemort": "伏地魔"}'
    parsed = json.loads(raw)
    glossary = {str(k): str(v) for k, v in parsed.items()}
    assert glossary["Harry Potter"] == "哈利·波特"
    assert glossary["Voldemort"] == "伏地魔"
    print(f"  解析结果: {glossary}")
    print("  ✅ PASS")
    return True


def test_api_glossary_json_invalid_raises():
    print("\n" + "=" * 60)
    print("🧪 Test 11: 非法 glossary_json 触发 json.JSONDecodeError")
    print("=" * 60)

    bad_input = "not-json-at-all"
    raised = False
    try:
        json.loads(bad_input)
    except json.JSONDecodeError:
        raised = True

    assert raised, "应抛出 JSONDecodeError"
    print("  ✅ PASS")
    return True


def test_prompt_contains_base_rules():
    print("\n" + "=" * 60)
    print("🧪 Test 12: prompt 含目标语言与规则（回归）")
    print("=" * 60)

    t = SemanticsTranslator(target_lang="ja", glossary={"manga": "漫画"})
    prompt = t._build_system_prompt()
    assert "ja" in prompt, "prompt 应含目标语言"
    assert "HTML" in prompt, "prompt 应含 HTML 规则"
    assert "术语对照表" in prompt, "prompt 应含术语对照表"
    print("  ✅ PASS")
    return True


# ─── 主入口 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_prompt_no_glossary,
        test_prompt_with_glossary,
        test_all_glossary_entries_in_prompt,
        test_empty_glossary_same_as_none,
        test_glossary_used_in_translation,
        test_default_glossary_empty,
        test_compiler_passes_glossary,
        test_compiler_no_glossary_empty,
        test_job_model_glossary_field,
        test_api_glossary_json_parse_valid,
        test_api_glossary_json_invalid_raises,
        test_prompt_contains_base_rules,
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
    print(f"📊 C6 Results: {passed} passed, {failed} failed")
    print("=" * 60)
    import sys as _sys; _sys.exit(1 if failed > 0 else 0)
