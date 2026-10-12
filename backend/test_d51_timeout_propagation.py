"""Control-flow exceptions must reach the job runner instead of safe fallbacks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.cancellation import JobCancelled
import app.domain.book_profile_service as profiler
import app.engine.compiler as compiler_module
from app.engine.compiler import ExtremeCompiler


INTERRUPTIONS = (JobCancelled, SoftTimeLimitExceeded)


@pytest.fixture
def compiler(tmp_path):
    return ExtremeCompiler(str(tmp_path / "source.epub"), str(tmp_path / "output.epub"))


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_preprocessing_interrupt_does_not_enter_safe_mode(compiler, monkeypatch, interruption):
    error = interruption("stop")
    monkeypatch.setattr(compiler, "_run_full_pipeline", Mock(side_effect=error))
    safe = Mock(return_value=True)
    monkeypatch.setattr(compiler, "_run_safe_mode", safe)
    with pytest.raises(interruption) as raised:
        compiler.run()
    assert raised.value is error
    safe.assert_not_called()


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_safe_preprocessing_interrupt_propagates(compiler, monkeypatch, interruption):
    monkeypatch.setattr(compiler, "_run_full_pipeline", Mock(side_effect=ValueError("malformed source")))
    monkeypatch.setattr(compiler, "_run_safe_mode", Mock(side_effect=interruption("stop")))
    with pytest.raises(interruption):
        compiler.run()


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_cleaner_interrupt_is_not_skipped_or_downgraded(compiler, monkeypatch, interruption):
    doc = SimpleNamespace(get_type=lambda: 9, get_name=lambda: "chapter.xhtml",
                          get_content=lambda: b"<html><body><p>Source text.</p></body></html>")
    book = SimpleNamespace(get_items=lambda: [doc])
    monkeypatch.setattr(compiler_module, "EpubUnpacker",
                        lambda _path: SimpleNamespace(load_book=lambda: book, source_warnings=[]))
    cleaner = SimpleNamespace(process=Mock(side_effect=interruption("stop")))
    compiler.cleaners = [cleaner]
    safe = Mock(return_value=True)
    monkeypatch.setattr(compiler, "_run_safe_mode", safe)
    with pytest.raises(interruption):
        compiler.run()
    cleaner.process.assert_called_once()
    safe.assert_not_called()


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_glossary_preprocessing_interrupt_is_not_ignored(compiler, monkeypatch, interruption):
    monkeypatch.setattr(compiler, "stage_callback", Mock(side_effect=interruption("stop")))
    with pytest.raises(interruption):
        compiler._build_and_inject_auto_glossary([])


def test_normal_preprocessing_errors_keep_existing_safe_fallback(compiler, monkeypatch):
    monkeypatch.setattr(compiler, "_run_full_pipeline", Mock(side_effect=ValueError("malformed source")))
    safe = Mock(return_value=True)
    monkeypatch.setattr(compiler, "_run_safe_mode", safe)
    assert compiler.run() is True
    safe.assert_called_once()


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_profile_metadata_interrupt_propagates(monkeypatch, interruption):
    monkeypatch.setattr(profiler, "EpubUnpacker",
                        lambda _path: SimpleNamespace(load_book=Mock(side_effect=interruption("stop"))))
    with pytest.raises(interruption):
        profiler._extract_metadata_and_toc("unused.epub")


@pytest.fixture
def profile_request(monkeypatch):
    for name, value in {
        "OPENAI_API_KEY": "offline-test", "OPENAI_BASE_URL": "https://mock.invalid/v1",
        "EPUB_BOOK_PROFILER_ENABLED": "1", "EPUB_BOOK_PROFILER_MODEL": "deepseek-flash",
        "OPENAI_DISABLE_JSON_RESPONSE_FORMAT": "0",
    }.items():
        monkeypatch.setenv(name, value)
    payload = {"metadata": {}, "toc": [], "samples": [{"text": "A bounded source sample."}]}
    monkeypatch.setattr(profiler, "build_profiler_input", lambda **_: (payload, {}))
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    http_client = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(profiler, "AsyncOpenAI", lambda **_: client)
    monkeypatch.setattr(profiler.httpx, "AsyncClient", lambda **_: http_client)

    async def passthrough(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(profiler, "governed_request", passthrough)
    return SimpleNamespace(create=create, client=client, http_client=http_client)


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_profile_request_interrupt_does_not_use_heuristic_fallback(profile_request, interruption):
    # Even a cancellation reason containing the compatibility keyword must stop.
    error = interruption("response_format stop")
    profile_request.create.side_effect = error
    with pytest.raises(interruption) as raised:
        asyncio.run(profiler.profile_book_async(epub_path="unused.epub", manifest={}, model="deepseek-flash"))
    assert raised.value is error
    assert profile_request.create.await_count == 1
    profile_request.client.close.assert_awaited_once()
    profile_request.http_client.aclose.assert_awaited_once()


def test_normal_profile_error_keeps_existing_heuristic_fallback(profile_request):
    profile_request.create.side_effect = RuntimeError("provider temporarily unavailable")
    result = asyncio.run(profiler.profile_book_async(epub_path="unused.epub", manifest={}, model="deepseek-flash"))
    assert result["status"] == "fallback"
    assert profile_request.create.await_count == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
