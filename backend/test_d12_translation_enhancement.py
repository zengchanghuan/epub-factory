"""
D12 测试：翻译链路增强

- _looks_like_error_response 结果校验
- _candidate_routes 主/备 base_url、model 组合
"""

import os
import sys
import asyncio
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.engine.cleaners.semantics_translator import SemanticsTranslator


def test_looks_like_error_response():
    assert SemanticsTranslator._looks_like_error_response("Error: rate limit") is True
    assert SemanticsTranslator._looks_like_error_response("Sorry, I cannot translate.") is True
    assert SemanticsTranslator._looks_like_error_response("I'm unable to process") is True
    assert SemanticsTranslator._looks_like_error_response("<p>Normal translation</p>") is False
    assert SemanticsTranslator._looks_like_error_response("") is False


def test_faithful_translation_prompt_constraints():
    """系统 prompt 必须锁定忠实翻译，不鼓励信达雅式改写。"""
    t = SemanticsTranslator(target_lang="zh-CN", glossary={"Smith": "史密斯"})
    prompt = t._build_system_prompt()
    assert "忠实翻译" in prompt
    assert "不得删减" in prompt
    assert "不得删减、总结、解释、本土化、审查、弱化或替作者表达" in prompt
    assert "政治性、宗教性、争议性内容" in prompt
    assert "不追求“信达雅”式改写" in prompt
    assert "禁止译名漂移" in prompt
    assert "Smith → 史密斯" in prompt


def test_candidate_routes_default():
    """无 fallback 时仅返回主 base_url + 主 model。"""
    t = SemanticsTranslator(target_lang="zh-CN")
    routes = t._candidate_routes()
    assert len(routes) >= 1
    assert routes[0] == (t.base_url, t.model)


def test_candidate_routes_with_fallbacks():
    """有 OPENAI_BASE_URL_FALLBACKS / OPENAI_MODEL_FALLBACKS 时路由含备选。"""
    os.environ["OPENAI_BASE_URL_FALLBACKS"] = "https://fallback1.com/v1,https://fallback2.com/v1"
    os.environ["OPENAI_MODEL_FALLBACKS"] = "deepseek-chat"
    try:
        t = SemanticsTranslator(target_lang="zh-CN")
        routes = t._candidate_routes()
        assert len(routes) >= 2
        bases = {r[0] for r in routes}
        models = {r[1] for r in routes}
        assert t.base_url in bases
        assert "https://fallback1.com/v1" in bases or "https://fallback2.com/v1" in bases
        assert t.model in models
        assert "deepseek-chat" in models
    finally:
        os.environ.pop("OPENAI_BASE_URL_FALLBACKS", None)
        os.environ.pop("OPENAI_MODEL_FALLBACKS", None)


def test_translation_stability_caps_env_concurrency_and_batch_size():
    """线上即使 .env 写高并发，也应被稳定性 cap 限住。"""
    old_values = {
        "OPENAI_CONCURRENCY": os.environ.get("OPENAI_CONCURRENCY"),
        "EPUB_TRANSLATION_CONCURRENCY_CAP": os.environ.get("EPUB_TRANSLATION_CONCURRENCY_CAP"),
        "EPUB_TRANSLATION_BATCH_MAX_CHARS": os.environ.get("EPUB_TRANSLATION_BATCH_MAX_CHARS"),
        "EPUB_TRANSLATION_BATCH_MAX_CHARS_CAP": os.environ.get("EPUB_TRANSLATION_BATCH_MAX_CHARS_CAP"),
    }
    os.environ["OPENAI_CONCURRENCY"] = "12"
    os.environ["EPUB_TRANSLATION_CONCURRENCY_CAP"] = "3"
    os.environ["EPUB_TRANSLATION_BATCH_MAX_CHARS"] = "12000"
    os.environ["EPUB_TRANSLATION_BATCH_MAX_CHARS_CAP"] = "5000"
    try:
        t = SemanticsTranslator(target_lang=f"zh-CN-test-{uuid.uuid4().hex[:8]}")
        assert t.semaphore._value == 3
        assert t.batch_max_chars == 5000
        assert t.max_retries >= 4
        assert t.quality_retries >= 1
        assert t.request_timeout >= 90
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_translate_many_chunks_uses_one_json_batch():
    """多个未缓存 chunk 应合并为一次 JSON batch 请求。"""
    t = SemanticsTranslator(target_lang=f"zh-CN-test-{uuid.uuid4().hex[:8]}")
    calls = []

    async def fake_call(payload):
        calls.append(payload)
        return (
            {item["id"]: f"译文:{item['html']}" for item in payload},
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 30, "completion_tokens": 60},
        )

    t._call_llm_json_batch = fake_call
    chunks = [
        f"<p>Alice sees Smith {uuid.uuid4().hex}</p>",
        f"<p>Bob visits London {uuid.uuid4().hex}</p>",
    ]
    out = asyncio.run(t.translate_many_chunks_async(chunks))
    assert len(calls) == 1
    assert len(calls[0]) == 2
    assert out[0].translated_html.startswith("译文:")
    assert out[1].translated_html.startswith("译文:")
    assert t.stats.api_calls == 0  # fake_call 不更新 api_calls；此处只验证批处理编排
    assert t.stats.translated_chunks == 2


def test_translate_many_chunks_error_like_only_fails_that_chunk():
    """批内单块返回错误响应时，只标记该块失败，其余正常译文不受连累。"""
    t = SemanticsTranslator(target_lang=f"zh-CN-test-{uuid.uuid4().hex[:8]}")

    async def fake_call(payload):
        out = {}
        for item in payload:
            if "BAD" in item["html"]:
                out[item["id"]] = "Sorry, I cannot translate this content."
            else:
                out[item["id"]] = f"译文:{item['html']}"
        return (out, {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 30, "completion_tokens": 60})

    t._call_llm_json_batch = fake_call
    chunks = [
        f"<p>Alice sees Smith {uuid.uuid4().hex}</p>",
        f"<p>BAD chunk {uuid.uuid4().hex}</p>",
        f"<p>Bob visits London {uuid.uuid4().hex}</p>",
    ]
    out = asyncio.run(t.translate_many_chunks_async(chunks))

    assert out[0].error is None and out[0].translated_html.startswith("译文:")
    assert out[2].error is None and out[2].translated_html.startswith("译文:")
    # 出错块回退原文（inner_html），且被标记 error
    assert out[1].error is not None
    assert not out[1].translated_html.startswith("译文:")
    # 统计：2 成功 1 失败，无双计
    assert t.stats.translated_chunks == 2
    assert t.stats.failed_chunks == 1
    assert t.stats.translated_chunks + t.stats.failed_chunks == t.stats.total_chunks


def test_translate_many_chunks_splits_failed_batch():
    """整批失败时应自动拆小重试，避免一批原文全部回写。"""
    t = SemanticsTranslator(target_lang=f"zh-CN-test-{uuid.uuid4().hex[:8]}")
    calls = []
    progress = []
    t.progress_callback = progress.append

    async def fake_call(payload):
        calls.append(len(payload))
        if len(payload) > 1:
            raise ValueError("batch JSON parse failed")
        return (
            {0: "译文：这是一段已经翻译完成的中文内容。"},
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 10, "completion_tokens": 20},
        )

    t._call_llm_json_batch = fake_call
    chunks = [
        f"<p>Alice sees Smith {uuid.uuid4().hex}</p>",
        f"<p>Bob visits London {uuid.uuid4().hex}</p>",
    ]
    out = asyncio.run(t.translate_many_chunks_async(chunks))

    assert calls[0] == 2
    assert calls.count(1) == 2
    assert all(item.error is None for item in out)
    assert all(item.translated_html.startswith("译文") for item in out)
    assert t.stats.translated_chunks == 2
    assert t.stats.failed_chunks == 0
    assert any("批量翻译失败，拆分重试" in msg for msg in progress)


def test_translate_many_chunks_retries_untranslated_response():
    """模型若返回英文原文，不应缓存为成功译文，应单段补译。"""
    t = SemanticsTranslator(target_lang=f"zh-CN-test-{uuid.uuid4().hex[:8]}")
    original_inner = "This account of the discovery of DNA is unique in several important ways."
    calls = []

    async def fake_call(payload):
        calls.append(payload)
        if len(calls) == 1:
            return (
                {0: original_inner},
                {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 10, "completion_tokens": 20},
            )
        return (
            {0: "这段关于 DNA 发现过程的记述在几个重要方面都很独特。"},
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 10, "completion_tokens": 20},
        )

    t._call_llm_json_batch = fake_call
    out = asyncio.run(t.translate_many_chunks_async([f"<p>{original_inner}</p>"]))

    assert len(calls) == 2
    assert out[0].error is None
    assert "独特" in out[0].translated_html
    assert t.stats.translated_chunks == 1
    assert t.stats.failed_chunks == 0


def test_translate_many_chunks_uses_multiple_quality_retries():
    """单段补译第一次仍返回原文时，应继续质量重试。"""
    old_value = os.environ.get("EPUB_TRANSLATION_QUALITY_RETRIES")
    os.environ["EPUB_TRANSLATION_QUALITY_RETRIES"] = "2"
    try:
        t = SemanticsTranslator(target_lang=f"zh-CN-test-{uuid.uuid4().hex[:8]}")
    finally:
        if old_value is None:
            os.environ.pop("EPUB_TRANSLATION_QUALITY_RETRIES", None)
        else:
            os.environ["EPUB_TRANSLATION_QUALITY_RETRIES"] = old_value

    original_inner = "This account of the discovery of DNA is unique in several important ways."
    calls = []

    async def fake_call(payload):
        calls.append(payload)
        if len(calls) < 3:
            return (
                {0: original_inner},
                {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 10, "completion_tokens": 20},
            )
        return (
            {0: "这段关于 DNA 发现过程的记述在几个重要方面都很独特。"},
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 10, "completion_tokens": 20},
        )

    t._call_llm_json_batch = fake_call
    out = asyncio.run(t.translate_many_chunks_async([f"<p>{original_inner}</p>"]))

    assert len(calls) == 3
    assert out[0].error is None
    assert "独特" in out[0].translated_html
    assert out[0].retry_count == 2
    assert t.stats.translated_chunks == 1
    assert t.stats.failed_chunks == 0


def test_translate_many_chunks_emits_final_failure_progress():
    """单段补译最终失败时，UI progress 应能看到失败原因。"""
    old_value = os.environ.get("EPUB_TRANSLATION_QUALITY_RETRIES")
    os.environ["EPUB_TRANSLATION_QUALITY_RETRIES"] = "1"
    try:
        t = SemanticsTranslator(target_lang=f"zh-CN-test-{uuid.uuid4().hex[:8]}")
    finally:
        if old_value is None:
            os.environ.pop("EPUB_TRANSLATION_QUALITY_RETRIES", None)
        else:
            os.environ["EPUB_TRANSLATION_QUALITY_RETRIES"] = old_value

    original_inner = "This account of the discovery of DNA is unique in several important ways."
    progress = []
    t.progress_callback = progress.append

    async def fake_call(payload):
        return (
            {item["id"]: item["html"] for item in payload},
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 10, "completion_tokens": 20},
        )

    t._call_llm_json_batch = fake_call
    out = asyncio.run(t.translate_many_chunks_async([f"<p>{original_inner}</p>"]))

    assert out[0].error is not None
    assert any("段落质检未通过，启动单段补译" in msg for msg in progress)
    assert any("段落翻译最终失败，已回写原文" in msg for msg in progress)


def test_extract_json_tolerates_preamble_and_trailing_commas():
    """模型偶尔会包一层说明或留下尾随逗号，解析器应尽量容错。"""
    t = SemanticsTranslator(target_lang=f"zh-CN-test-{uuid.uuid4().hex[:8]}")
    parsed = t._extract_json_from_response(
        'Here is the JSON:\\n{"results":[{"id":0,"translation":"译文"},],}\\n'
    )
    assert parsed["results"][0]["translation"] == "译文"


def test_translate_many_chunks_retries_html_tag_mismatch():
    """模型若丢掉内联标签，应单段重试，而不是把坏 HTML 写入结果。"""
    t = SemanticsTranslator(target_lang=f"zh-CN-test-{uuid.uuid4().hex[:8]}")
    calls = []

    async def fake_call(payload):
        calls.append(payload)
        if len(calls) == 1:
            return (
                {0: "这是重要的。"},
                {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 10, "completion_tokens": 20},
            )
        return (
            {0: "这是<em>重要的</em>。"},
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 10, "completion_tokens": 20},
        )

    t._call_llm_json_batch = fake_call
    out = asyncio.run(t.translate_many_chunks_async(["<p>This is <em>important</em>.</p>"]))

    assert len(calls) == 2
    assert out[0].error is None
    assert "<em>" in out[0].translated_html
    assert t.stats.translated_chunks == 1
    assert t.stats.failed_chunks == 0


def _run():
    cases = [
        test_looks_like_error_response,
        test_faithful_translation_prompt_constraints,
        test_candidate_routes_default,
        test_candidate_routes_with_fallbacks,
        test_translation_stability_caps_env_concurrency_and_batch_size,
        test_translate_many_chunks_uses_one_json_batch,
        test_translate_many_chunks_error_like_only_fails_that_chunk,
        test_translate_many_chunks_splits_failed_batch,
        test_translate_many_chunks_retries_untranslated_response,
        test_translate_many_chunks_uses_multiple_quality_retries,
        test_translate_many_chunks_emits_final_failure_progress,
        test_extract_json_tolerates_preamble_and_trailing_commas,
        test_translate_many_chunks_retries_html_tag_mismatch,
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
