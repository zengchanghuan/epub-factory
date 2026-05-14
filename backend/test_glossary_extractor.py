"""
术语抽取与校验单元测试。

覆盖：
1. 规则层抽取的命中率与去噪能力
2. 黑名单过滤（句首高频词、常用代词）
3. 称谓后人名识别（高置信度路径）
4. 频次阈值
5. Verify 修正逻辑（正确译名已用 / 残留英文 / 完全找不到）
6. merge_glossaries 优先级（用户 > 自动）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.engine.glossary_extractor import (
    extract_candidates,
    verify_and_fix,
    merge_glossaries,
)


SAMPLE_TEXT = """
Mr. Smith walked into the room. The room was dark and cold.
"Hello, Mr. Smith," said Mrs. Smith with a smile.
Harry Potter looked up at Hermione Granger. Hogwarts was their home.
"Where is Harry Potter?" Hermione Granger asked. They were both at Hogwarts.
Harry Potter and Hermione Granger walked together to Hogwarts.
The NASA scientists were excited. NASA had made a breakthrough.
And then they left. But the door was still open.
"""


def test_extract_basic_names():
    texts = [SAMPLE_TEXT]
    candidates, stats = extract_candidates(texts, min_count=2)
    terms = {c.term for c in candidates}

    # 必须命中的高频名词
    assert "Smith" in terms, f"Smith 未命中, 实际: {terms}"
    assert "Harry Potter" in terms, f"Harry Potter 未命中, 实际: {terms}"
    assert "Hermione Granger" in terms, f"Hermione Granger 未命中, 实际: {terms}"
    assert "Hogwarts" in terms, "Hogwarts 未命中"
    assert "NASA" in terms, "NASA 缩写未命中"
    # 称谓本身不应入选
    assert "Mr" not in terms and "Mrs" not in terms, f"称谓不应入选, 实际: {terms}"

    print(f"✓ 基本命中测试通过, 共 {stats.final_kept} 个术语: {terms}")


def test_blacklist_filter():
    """句首高频词如 The、And、But 不应混入。"""
    text = "The boy ran. And then he stopped. But it was too late. The end."
    candidates, _ = extract_candidates([text], min_count=2)
    terms = {c.term for c in candidates}
    for forbidden in ("The", "And", "But"):
        assert forbidden not in terms, f"{forbidden} 不应出现在术语库, 实际: {terms}"
    print("✓ 黑名单过滤通过")


def test_honorific_high_confidence():
    """称谓后接的词应有最高置信度。"""
    text = "Mr. Jones met Mr. Jones again. Mr. Jones was happy."
    candidates, _ = extract_candidates([text], min_count=2)
    jones = next((c for c in candidates if c.term == "Jones"), None)
    assert jones is not None, "Jones 未识别"
    assert jones.confidence >= 0.7, f"Jones 置信度过低: {jones.confidence}"
    assert "honorific" in jones.kinds
    print(f"✓ 称谓识别置信度: {jones.confidence}")


def test_min_count_threshold():
    """只出现 1 次的不应入选。"""
    text = "Bilbo lived in a hole. The hobbit was happy. He met Gandalf. Gandalf was wise. Gandalf smiled."
    candidates, _ = extract_candidates([text], min_count=2)
    terms = {c.term for c in candidates}
    assert "Bilbo" not in terms, "Bilbo 只出现 1 次, 不应入选"
    assert "Gandalf" in terms, "Gandalf 出现 3 次, 应入选"
    print("✓ 频次阈值通过")


def test_max_terms_cap():
    """术语数应被截断到 max_terms。"""
    # 制造 100 个不同的高频名词（纯字母，避免被正则过滤）
    import string
    def name(i: int) -> str:
        a = string.ascii_uppercase[i % 26]
        b = string.ascii_uppercase[(i // 26) % 26]
        return f"{a}aron {b}aker"   # Aaron Aaker / Baron Aaker / Caron Aaker ...

    seen = set()
    parts = []
    for i in range(100):
        n = name(i)
        if n in seen:
            continue
        seen.add(n)
        parts.append(f"{n} did something. {n} was important.")
    text = " ".join(parts)
    candidates, _ = extract_candidates([text], min_count=2, max_terms=20)
    assert len(candidates) == 20, f"应截断到 20 个, 实际 {len(candidates)}"
    print(f"✓ 上限截断通过 (输入 {len(seen)} 个唯一术语, 截断到 20)")


def test_verify_correct_translation_kept():
    """译文已正确使用译名时，不应改动。"""
    glossary = {"Smith": "史密斯"}
    original = "Mr. Smith walked in. Smith was tired."
    translated = "史密斯先生走了进来。史密斯很累。"
    fixed, result = verify_and_fix(original, translated, glossary)
    assert fixed == translated
    assert result.fixed_count == 0
    print("✓ 正确译名不被改动")


def test_verify_residual_english_replaced():
    """LLM 漏译留下英文 Smith 时，应替换为译名。"""
    glossary = {"Smith": "史密斯"}
    original = "Mr. Smith walked in."
    translated = "Smith 先生走了进来。"  # LLM 漏译
    fixed, result = verify_and_fix(original, translated, glossary)
    assert "史密斯" in fixed, f"应修正为译名, 实际: {fixed}"
    assert "Smith" not in fixed, f"原文不应残留, 实际: {fixed}"
    assert result.fixed_count == 1
    print(f"✓ 残留英文修正: {translated} → {fixed}")


def test_verify_unfixable_logged():
    """LLM 用了完全不同的音译时，记录为 unfixable。"""
    glossary = {"Smith": "史密斯"}
    original = "Mr. Smith walked in."
    translated = "斯密思先生走了进来。"  # LLM 用了奇怪音译
    fixed, result = verify_and_fix(original, translated, glossary)
    # 不会自动修正（无法可靠定位"斯密思"对应 Smith）
    assert "斯密思" in fixed
    # 但应记录到 unfixable_examples 供运维观测
    assert len(result.unfixable_examples) > 0
    print(f"✓ 无法自动修正时记录观测: {result.unfixable_examples}")


def test_merge_glossaries_user_priority():
    """用户手填的应覆盖自动抽取的。"""
    auto = {"Smith": "史密斯", "Jones": "琼斯"}
    user = {"Smith": "斯密斯（自定义）"}
    merged = merge_glossaries(user, auto)
    assert merged["Smith"] == "斯密斯（自定义）"
    assert merged["Jones"] == "琼斯"
    print("✓ 用户优先级生效")


def test_word_boundary_no_overmatch():
    """整词匹配不应误改 'Brand' 里的 'And'。"""
    glossary = {"And": "和"}
    original = "Brand new. And then. Sandwich."
    translated = "Brand new. And then. Sandwich."
    fixed, _ = verify_and_fix(original, translated, glossary)
    # Brand / Sandwich 不应被改
    assert "Brand" in fixed
    assert "Sandwich" in fixed
    # "And" 单词如果在译文中出现，应被替换
    print(f"✓ 整词边界: {fixed}")


if __name__ == "__main__":
    test_extract_basic_names()
    test_blacklist_filter()
    test_honorific_high_confidence()
    test_min_count_threshold()
    test_max_terms_cap()
    test_verify_correct_translation_kept()
    test_verify_residual_english_replaced()
    test_verify_unfixable_logged()
    test_merge_glossaries_user_priority()
    test_word_boundary_no_overmatch()
    print("\n所有用例通过 ✓")
