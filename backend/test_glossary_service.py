import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import app.engine.glossary_service as glossary_service
from app.engine.glossary_extractor import GlossaryCandidate


def test_filter_candidates_removes_noise_and_possessives():
    candidates = [
        GlossaryCandidate("Prior’s", 5, 0.9, {"honorific"}),
        GlossaryCandidate("THE", 10, 0.7, {"acronym"}),
        GlossaryCandidate("CPA", 10, 0.7, {"acronym"}),
    ]
    filtered = glossary_service.filter_candidates(candidates)
    terms = [c.term for c in filtered]
    assert "Prior" in terms
    assert "CPA" in terms
    assert "THE" not in terms


def test_build_consistent_glossary_priority():
    old_translate = glossary_service.translate_glossary
    old_global = glossary_service.load_global_glossary

    async def fake_translate(candidates, **kwargs):
        return {"Smith": "自动史密斯", "Jones": "琼斯"}

    glossary_service.translate_glossary = fake_translate
    glossary_service.load_global_glossary = lambda target_lang="zh-CN": {
        "Smith": "全局史密斯",
        "London": "伦敦",
    }
    try:
        result = glossary_service.build_consistent_glossary(
            ["Mr. Smith met Mr. Smith in London. Jones saw Jones."],
            user_glossary={"Smith": "用户史密斯"},
            min_count=2,
        )
        assert result.glossary["Smith"] == "用户史密斯"
        assert result.glossary["London"] == "伦敦"
        assert result.glossary["Jones"] == "琼斯"
        assert result.stats["merged_glossary_count"] == 3
    finally:
        glossary_service.translate_glossary = old_translate
        glossary_service.load_global_glossary = old_global


if __name__ == "__main__":
    test_filter_candidates_removes_noise_and_possessives()
    print("  ✅ test_filter_candidates_removes_noise_and_possessives")
    test_build_consistent_glossary_priority()
    print("  ✅ test_build_consistent_glossary_priority")
