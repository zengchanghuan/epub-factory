"""Shared physical-request RPM/TPM gate. Estimates are never provider bills."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import math
import os
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

from app.cancellation import raise_if_cancelled

logger = logging.getLogger("epub_factory.llm_token_bucket")


class GatewayControlError(BaseException):
    """Local dispatch refusal, never a provider retry/fallback signal."""


LLMGatewayError = GatewayControlError


class GatewayConfigurationError(GatewayControlError):
    pass


class GatewayUnavailable(GatewayControlError):
    pass


class GatewayRequestTooLarge(GatewayControlError):
    pass


class GatewayWaitTimeout(GatewayControlError):
    pass


def route_identity(base_url: str, model: str) -> tuple[str, str]:
    """Quota identity excludes credentials, stage, job and caller route labels."""
    host = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    if not host:
        raise GatewayConfigurationError("模型服务地址缺少有效主机名，未发起请求")
    model = str(model or "").strip().lower()
    if model in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
        model = "deepseek-flash"
    return host, model


_ACQUIRE_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local req_capacity = tonumber(ARGV[1])
local token_capacity = tonumber(ARGV[2])
local token_cost = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local identity = ARGV[5]
if redis.call('EXISTS', KEYS[3]) == 1 then return {1, 0} end
local function refill(key, capacity)
  local data = redis.call('HMGET', key, 'tokens', 'updated')
  local tokens = tonumber(data[1]) or capacity
  local updated = tonumber(data[2]) or now
  return math.min(capacity, tokens + math.max(0, now - updated) * capacity / 60000)
end
local req = refill(KEYS[1], req_capacity)
local tokens = refill(KEYS[2], token_capacity)
local generation = redis.call('HGET', KEYS[2], 'generation') or identity
local allowed = req >= 1 and tokens >= token_cost
local wait = 0
if allowed then
  req = req - 1
  tokens = tokens - token_cost
  redis.call('HSET', KEYS[3], 'estimated', token_cost, 'settled', 0, 'generation', generation)
  redis.call('PEXPIRE', KEYS[3], ttl)
else
  wait = math.ceil(math.max(math.max(0, 1 - req) * 60000 / req_capacity,
                           math.max(0, token_cost - tokens) * 60000 / token_capacity))
end
redis.call('HSET', KEYS[1], 'tokens', req, 'updated', now)
redis.call('HSET', KEYS[2], 'tokens', tokens, 'updated', now, 'generation', generation)
redis.call('PEXPIRE', KEYS[1], ttl)
redis.call('PEXPIRE', KEYS[2], ttl)
if allowed then return {1, 0} end
return {0, wait}
"""

_ADJUST_LUA = """
local lease = redis.call('HMGET', KEYS[2], 'estimated', 'settled', 'generation')
if not lease[1] or lease[2] == '1' then return 0 end
redis.call('HSET', KEYS[2], 'settled', 1)
if redis.call('HGET', KEYS[1], 'generation') ~= lease[3] then return 0 end
local current = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
if not current then return 0 end
redis.call('HSET', KEYS[1], 'tokens', math.min(tonumber(ARGV[1]), current + tonumber(lease[1]) - tonumber(ARGV[2])))
return 1
"""


@dataclass(frozen=True)
class TokenBucketLease:
    estimated_tokens: int = 0
    waited_ms: int = 0
    enabled: bool = False
    token_key: str = ""
    reservation_key: str = ""
    bypassed: bool = False


class DistributedLLMTokenBucket:
    def __init__(self) -> None:
        self.enabled = os.environ.get("EPUB_LLM_RATE_LIMITER_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
        self.fail_open = os.environ.get("EPUB_LLM_RATE_LIMIT_FAIL_OPEN", "0").lower() in {"1", "true", "yes", "on"}
        self.redis_url = (os.environ.get("REDIS_URL") or os.environ.get("CELERY_BROKER_URL") or "").strip()
        self.key_prefix = os.environ.get("EPUB_LLM_RATE_LIMIT_KEY_PREFIX", "epub:llm:bucket")
        self.rpm, self.tpm, self.max_wait_seconds = 0, 0, 90.0
        self._configuration_error = ""
        self._redis = None
        self._client_lock = threading.RLock()
        self._pending_io = set()
        self._close_when_idle = False
        if self.enabled:
            try:
                self.rpm = int(os.environ.get("EPUB_LLM_RPM", "0"))
                self.tpm = int(os.environ.get("EPUB_LLM_TPM", "0"))
                self.max_wait_seconds = float(os.environ.get("EPUB_LLM_RATE_LIMIT_MAX_WAIT", "90"))
                if (not self.redis_url or not 1 <= self.rpm <= 10**12 or not 1 <= self.tpm <= 10**12
                        or not math.isfinite(self.max_wait_seconds) or not 0 < self.max_wait_seconds <= 3600):
                    raise ValueError("incomplete limits")
                if urlsplit(self.redis_url).scheme not in {"redis", "rediss", "unix"}:
                    raise ValueError("unsupported Redis transport")
            except (TypeError, ValueError):
                self._configuration_error = "全局模型配额配置无效，未发起请求"

    @staticmethod
    def _route_key(provider: str, model: str) -> str:
        base = provider if "://" in provider else "https://" + provider
        route = route_identity(base, model)
        return hashlib.sha256("|".join(route).encode()).hexdigest()[:32]

    def _client(self):
        with self._client_lock:
            if self._redis is None:
                from redis import Redis
                self._redis = Redis.from_url(self.redis_url, decode_responses=True,
                                             socket_connect_timeout=1.5, socket_timeout=2.0)
            return self._redis

    def close(self):
        with self._client_lock:
            if self._pending_io:
                self._close_when_idle = True
                return
            client, self._redis = self._redis, None
            self._close_when_idle = False
        if client is not None:
            client.close()

    async def _eval_async(self, *args):
        marker = object()
        with self._client_lock:
            self._pending_io.add(marker)
        def work():
            try:
                return self._eval(*args)
            finally:
                with self._client_lock:
                    self._pending_io.discard(marker)
                    close = self._close_when_idle and not self._pending_io
                if close:
                    self.close()
        try:
            task = asyncio.get_running_loop().run_in_executor(None, work)
        except BaseException:
            with self._client_lock:
                self._pending_io.discard(marker)
            raise
        def finished(done):
            # Late I/O may reserve quota, but never dispatch or refund. Closing
            # an owned client waits for its actual I/O rather than task cancel.
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def _eval(self, *args):
        return self._client().eval(*args)

    def _prepare(self, provider, model, estimated_tokens):
        if self._configuration_error:
            raise GatewayConfigurationError(self._configuration_error)
        if isinstance(estimated_tokens, bool) or not isinstance(estimated_tokens, int) or estimated_tokens < 1:
            raise GatewayConfigurationError("模型请求配额估算无效，未发起请求")
        if estimated_tokens > self.tpm:
            raise GatewayRequestTooLarge("单次模型请求超过 TPM 配额，请调整分块或配额配置")
        route = self._route_key(provider, model)
        prefix = f"{self.key_prefix}:{{{route}}}"
        identity = uuid.uuid4().hex
        token_key, reservation_key = prefix + ":tokens", prefix + ":lease:" + identity
        # Covers the worker deadline. Expired leases cannot adjust a new bucket.
        args = (_ACQUIRE_LUA, 3, prefix + ":requests", token_key, reservation_key,
                self.rpm, self.tpm, estimated_tokens, 7_200_000, identity)
        return args, token_key, reservation_key

    def _failure(self, exc):
        if not self.fail_open:
            raise GatewayUnavailable("全局模型配额服务暂不可用，已停止新增模型请求") from exc
        logger.warning("LLM quota explicitly bypassed (%s)", type(exc).__name__)
        return TokenBucketLease(bypassed=True)

    @staticmethod
    def _is_control(exc):
        from billiard.exceptions import SoftTimeLimitExceeded
        from app.cancellation import JobCancelled
        return isinstance(exc, (SoftTimeLimitExceeded, JobCancelled))

    async def acquire(self, *, provider: str, model: str, estimated_tokens: int,
                      cancel_check: Callable[[], bool] | None = None) -> TokenBucketLease:
        raise_if_cancelled(cancel_check)
        if not self.enabled:
            return TokenBucketLease()
        args, token_key, reservation_key = self._prepare(provider, model, estimated_tokens)
        started = time.monotonic()
        while True:
            raise_if_cancelled(cancel_check)
            try:
                result = await self._eval_async(*args)
            except Exception as exc:
                if self._is_control(exc):
                    raise
                return self._failure(exc)
            if int(result[0]):
                return TokenBucketLease(estimated_tokens, int((time.monotonic() - started) * 1000),
                                        True, token_key, reservation_key)
            delay = max(.01, min(float(result[1]) / 1000, .25))
            if time.monotonic() - started + delay > self.max_wait_seconds:
                raise GatewayWaitTimeout("等待全局模型配额超时，未发起请求")
            await asyncio.sleep(delay)

    def acquire_sync(self, *, provider: str, model: str, estimated_tokens: int,
                     cancel_check: Callable[[], bool] | None = None) -> TokenBucketLease:
        raise_if_cancelled(cancel_check)
        if not self.enabled:
            return TokenBucketLease()
        args, token_key, reservation_key = self._prepare(provider, model, estimated_tokens)
        started = time.monotonic()
        while True:
            raise_if_cancelled(cancel_check)
            try:
                result = self._eval(*args)
            except Exception as exc:
                if self._is_control(exc):
                    raise
                return self._failure(exc)
            if int(result[0]):
                return TokenBucketLease(estimated_tokens, int((time.monotonic() - started) * 1000),
                                        True, token_key, reservation_key)
            delay = max(.01, min(float(result[1]) / 1000, .25))
            if time.monotonic() - started + delay > self.max_wait_seconds:
                raise GatewayWaitTimeout("等待全局模型配额超时，未发起请求")
            time.sleep(delay)

    def _reconcile_args(self, lease, actual_tokens):
        if not lease.enabled or not lease.reservation_key:
            return None
        if isinstance(actual_tokens, bool) or not isinstance(actual_tokens, int) or actual_tokens < 0:
            return None
        return (_ADJUST_LUA, 2, lease.token_key, lease.reservation_key, self.tpm, actual_tokens)

    async def reconcile(self, lease: TokenBucketLease, *, actual_tokens: int) -> None:
        args = self._reconcile_args(lease, actual_tokens)
        if args:
            try:
                await self._eval_async(*args)
            except Exception as exc:
                if self._is_control(exc):
                    raise
                self._failure(exc)

    def reconcile_sync(self, lease: TokenBucketLease, *, actual_tokens: int) -> None:
        args = self._reconcile_args(lease, actual_tokens)
        if args:
            try:
                self._eval(*args)
            except Exception as exc:
                if self._is_control(exc):
                    raise
                self._failure(exc)


def estimate_request_tokens(system_prompt: str, user_content: str) -> int:
    """Historical estimate retained for compatibility, not billed usage."""
    return max(1, math.ceil((len(system_prompt or "") + len(user_content or "")) / 3)) + max(128, math.ceil(len(user_content or "") / 3))
