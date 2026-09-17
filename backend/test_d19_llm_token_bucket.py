"""D19: distributed LLM token bucket feature gate and token estimation."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.infra.llm_token_bucket import (
    DistributedLLMTokenBucket,
    estimate_request_tokens,
)


def test_limiter_is_safe_noop_without_explicit_enable():
    old = os.environ.get("EPUB_LLM_RATE_LIMITER_ENABLED")
    os.environ["EPUB_LLM_RATE_LIMITER_ENABLED"] = "0"
    try:
        limiter = DistributedLLMTokenBucket()
        lease = asyncio.run(limiter.acquire(
            provider="deepseek",
            model="deepseek-v4-flash",
            estimated_tokens=1000,
        ))
    finally:
        if old is None:
            os.environ.pop("EPUB_LLM_RATE_LIMITER_ENABLED", None)
        else:
            os.environ["EPUB_LLM_RATE_LIMITER_ENABLED"] = old
    assert lease.enabled is False
    assert lease.waited_ms == 0


def test_incomplete_configuration_disables_limiter():
    names = (
        "EPUB_LLM_RATE_LIMITER_ENABLED",
        "EPUB_LLM_RPM",
        "EPUB_LLM_TPM",
        "REDIS_URL",
        "CELERY_BROKER_URL",
    )
    previous = {name: os.environ.get(name) for name in names}
    try:
        os.environ["EPUB_LLM_RATE_LIMITER_ENABLED"] = "1"
        os.environ["EPUB_LLM_RPM"] = "0"
        os.environ["EPUB_LLM_TPM"] = "0"
        os.environ.pop("REDIS_URL", None)
        os.environ.pop("CELERY_BROKER_URL", None)
        limiter = DistributedLLMTokenBucket()
        assert limiter.enabled is False
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_token_estimate_grows_with_request_size():
    small = estimate_request_tokens("system", "short")
    large = estimate_request_tokens("system " * 100, "content " * 1000)
    assert small >= 128
    assert large > small


if __name__ == "__main__":
    tests = [
        test_limiter_is_safe_noop_without_explicit_enable,
        test_incomplete_configuration_disables_limiter,
        test_token_estimate_grows_with_request_size,
    ]
    for test_fn in tests:
        test_fn()
        print(f"  ✅ {test_fn.__name__}")
    print(f"\n📊 {len(tests)} passed, 0 failed")
