"""Optional Redis-backed token bucket shared by all translation workers.

The limiter is deliberately feature-gated.  Operators must configure both
``EPUB_LLM_RATE_LIMITER_ENABLED=1`` and positive RPM/TPM limits before it can
delay requests.  This keeps deployment safe while making the cross-worker
governor available for load testing and production rollout.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Callable

from app.cancellation import JobCancelled

logger = logging.getLogger("epub_factory.llm_token_bucket")

_ACQUIRE_LUA = """
local now = tonumber(ARGV[1])
local req_capacity = tonumber(ARGV[2])
local req_rate = tonumber(ARGV[3])
local token_capacity = tonumber(ARGV[4])
local token_rate = tonumber(ARGV[5])
local token_cost = tonumber(ARGV[6])
local ttl = tonumber(ARGV[7])

local function refill(key, capacity, rate)
  local data = redis.call('HMGET', key, 'tokens', 'updated')
  local tokens = tonumber(data[1])
  local updated = tonumber(data[2])
  if tokens == nil then tokens = capacity end
  if updated == nil then updated = now end
  if now > updated then
    tokens = math.min(capacity, tokens + ((now - updated) / 1000.0) * rate)
  end
  return tokens
end

local req_tokens = refill(KEYS[1], req_capacity, req_rate)
local llm_tokens = refill(KEYS[2], token_capacity, token_rate)
if req_tokens >= 1 and llm_tokens >= token_cost then
  redis.call('HMSET', KEYS[1], 'tokens', req_tokens - 1, 'updated', now)
  redis.call('HMSET', KEYS[2], 'tokens', llm_tokens - token_cost, 'updated', now)
  redis.call('PEXPIRE', KEYS[1], ttl)
  redis.call('PEXPIRE', KEYS[2], ttl)
  return {1, 0}
end

local req_wait = 0
if req_tokens < 1 then req_wait = ((1 - req_tokens) / req_rate) * 1000 end
local token_wait = 0
if llm_tokens < token_cost then token_wait = ((token_cost - llm_tokens) / token_rate) * 1000 end
local wait_ms = math.ceil(math.max(req_wait, token_wait))
redis.call('HMSET', KEYS[1], 'tokens', req_tokens, 'updated', now)
redis.call('HMSET', KEYS[2], 'tokens', llm_tokens, 'updated', now)
redis.call('PEXPIRE', KEYS[1], ttl)
redis.call('PEXPIRE', KEYS[2], ttl)
return {0, wait_ms}
"""

_ADJUST_LUA = """
local current = tonumber(redis.call('HGET', KEYS[1], 'tokens')) or 0
local capacity = tonumber(ARGV[1])
local adjustment = tonumber(ARGV[2])
redis.call('HSET', KEYS[1], 'tokens', math.min(capacity, current + adjustment))
return 1
"""


@dataclass(frozen=True)
class TokenBucketLease:
    estimated_tokens: int = 0
    waited_ms: int = 0
    enabled: bool = False
    token_key: str = ""


class DistributedLLMTokenBucket:
    def __init__(self) -> None:
        self.enabled = (
            os.environ.get("EPUB_LLM_RATE_LIMITER_ENABLED", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.rpm = max(0, int(os.environ.get("EPUB_LLM_RPM", "0")))
        self.tpm = max(0, int(os.environ.get("EPUB_LLM_TPM", "0")))
        self.redis_url = (
            os.environ.get("REDIS_URL")
            or os.environ.get("CELERY_BROKER_URL")
            or ""
        ).strip()
        self.key_prefix = os.environ.get("EPUB_LLM_RATE_LIMIT_KEY_PREFIX", "epub:llm:bucket")
        self.max_wait_seconds = max(
            1.0,
            float(os.environ.get("EPUB_LLM_RATE_LIMIT_MAX_WAIT", "90")),
        )
        self._redis = None
        self._disabled_reason = ""
        if self.enabled and (not self.redis_url or self.rpm <= 0 or self.tpm <= 0):
            self._disabled_reason = "Redis URL、RPM 或 TPM 未完整配置"
            self.enabled = False

    @staticmethod
    def _route_key(provider: str, model: str) -> str:
        raw = f"{provider or 'unknown'}:{model or 'unknown'}".lower()
        return re.sub(r"[^a-z0-9_.:-]+", "_", raw)[:160]

    async def _client(self):
        if self._redis is None:
            from redis.asyncio import Redis

            self._redis = Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=2.0,
            )
        return self._redis

    async def acquire(
        self,
        *,
        provider: str,
        model: str,
        estimated_tokens: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> TokenBucketLease:
        if not self.enabled:
            return TokenBucketLease()
        route = self._route_key(provider, model)
        req_key = f"{self.key_prefix}:{route}:requests"
        token_key = f"{self.key_prefix}:{route}:tokens"
        estimated = max(1, min(int(estimated_tokens), self.tpm))
        started = time.monotonic()
        try:
            client = await self._client()
            while True:
                if cancel_check and cancel_check():
                    raise JobCancelled("用户已停止翻译")
                now_ms = int(time.time() * 1000)
                result = await client.eval(
                    _ACQUIRE_LUA,
                    2,
                    req_key,
                    token_key,
                    now_ms,
                    self.rpm,
                    self.rpm / 60.0,
                    self.tpm,
                    self.tpm / 60.0,
                    estimated,
                    180_000,
                )
                allowed = bool(int(result[0]))
                if allowed:
                    return TokenBucketLease(
                        estimated_tokens=estimated,
                        waited_ms=int((time.monotonic() - started) * 1000),
                        enabled=True,
                        token_key=token_key,
                    )
                wait_seconds = max(0.05, min(float(result[1]) / 1000.0, 1.0))
                if time.monotonic() - started + wait_seconds > self.max_wait_seconds:
                    raise TimeoutError("global LLM token bucket wait exceeded configured limit")
                await asyncio.sleep(wait_seconds)
        except (JobCancelled, TimeoutError):
            raise
        except Exception as exc:
            # Redis failure must not make paid translation unavailable.  The
            # existing in-process adaptive limiter remains active.
            logger.warning("distributed LLM token bucket bypassed: %s", exc)
            self.enabled = False
            return TokenBucketLease()

    async def reconcile(self, lease: TokenBucketLease, *, actual_tokens: int) -> None:
        """Adjust the reservation to the provider's actual token usage."""
        if not lease.enabled or not self.enabled or not lease.token_key:
            return
        adjustment = lease.estimated_tokens - max(0, int(actual_tokens))
        if adjustment == 0:
            return
        try:
            client = await self._client()
            await client.eval(_ADJUST_LUA, 1, lease.token_key, self.tpm, adjustment)
        except Exception as exc:
            logger.warning("distributed LLM token reconciliation failed: %s", exc)


def estimate_request_tokens(system_prompt: str, user_content: str) -> int:
    """Conservative provider-agnostic reservation for input plus output."""
    input_chars = len(system_prompt or "") + len(user_content or "")
    input_tokens = max(1, math.ceil(input_chars / 3))
    output_chars = len(user_content or "")
    expected_output_tokens = max(128, math.ceil(output_chars / 3))
    return input_tokens + expected_output_tokens
