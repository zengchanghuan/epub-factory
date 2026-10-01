"""One governed boundary per physical model request, async and sync.

The gateway does not retry, interpret JSON, choose fallback models, or invent
billed usage. Those remain caller policy. A factory is invoked only after all
dispatch guards and the durable accounting reservation have succeeded.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import json
import logging
import math
import os
import sys
import time

from app.cancellation import raise_if_cancelled, JobCancelled
from .async_requests import bounded_request
from .llm_guard import assert_model_allowed
from .llm_route_health import DistributedRouteHealth
from .llm_token_bucket import (
    DistributedLLMTokenBucket, GatewayControlError, LLMGatewayError,
    GatewayConfigurationError, route_identity,
)
from .llm_usage_ledger import accounted_request, accounted_call, normalize_usage

logger = logging.getLogger("epub_factory.llm_gateway")
_dispatch_budget = ContextVar("llm_dispatch_budget", default=None)


@contextmanager
def dispatch_budget_scope(reserve_callback):
    token = _dispatch_budget.set(reserve_callback)
    try:
        yield
    finally:
        _dispatch_budget.reset(token)


def preflight_output_limit():
    try:
        value = int(os.environ.get("EPUB_PREFLIGHT_MAX_OUTPUT_TOKENS", "4096"))
        if not 1 <= value <= 16384:
            raise ValueError("out of bounds")
        return value
    except (TypeError, ValueError) as exc:
        raise GatewayConfigurationError("预分析输出预算配置无效，未发起模型请求") from exc


def _estimate(messages, max_output_tokens):
    try:
        if not isinstance(messages, (list, tuple)):
            raise ValueError("messages must be a sequence")
        content = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        if max_output_tokens is not None and (isinstance(max_output_tokens, bool)
                or not isinstance(max_output_tokens, int) or max_output_tokens < 1):
            raise ValueError("invalid output estimate")
        incoming = max(1, math.ceil(len(content.encode("utf-8")) / 3))
        return incoming + (max_output_tokens or max(128, incoming))
    except (TypeError, ValueError) as exc:
        raise GatewayConfigurationError("模型请求预算参数无效，未发起请求") from exc


def _guards(model, base_url, messages, cancel_check, timeout, max_output_tokens):
    raise_if_cancelled(cancel_check)
    assert_model_allowed(model, context="physical_request")
    route = route_identity(base_url, model)
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not math.isfinite(timeout) or timeout <= 0):
        raise GatewayConfigurationError("模型请求超时配置无效，未发起请求")
    return route, _estimate(messages, max_output_tokens)


def _before(estimated, *, model, base_url, messages, stage, max_output_tokens,
            cancel_check, before_dispatch, on_lease, lease):
    raise_if_cancelled(cancel_check)
    if on_lease:
        on_lease(lease)
    reserve = _dispatch_budget.get()
    if reserve:
        reserve(estimated, model=model, base_url=base_url, messages=messages,
                stage=stage, max_output_tokens=max_output_tokens)
    if before_dispatch:
        before_dispatch()
    raise_if_cancelled(cancel_check)


def _health(health, route, state, elapsed):
    try:
        if state.get("response") is not None:
            health.record_success(route, elapsed)
        elif state.get("transport_error") is not None:
            health.record_failure(route)
    except Exception as exc:
        from billiard.exceptions import SoftTimeLimitExceeded
        if isinstance(exc, (JobCancelled, SoftTimeLimitExceeded)):
            raise
        logger.warning("LLM advisory health unavailable (%s)", type(exc).__name__)


def _transport_error(exc):
    from billiard.exceptions import SoftTimeLimitExceeded
    return isinstance(exc, Exception) and not isinstance(exc, (JobCancelled, SoftTimeLimitExceeded))


def _close_owned(resource, primary):
    try:
        resource.close()
    except BaseException as exc:
        from billiard.exceptions import SoftTimeLimitExceeded
        is_control = not isinstance(exc, Exception) or isinstance(exc, (JobCancelled, SoftTimeLimitExceeded))
        if is_control and primary is None:
            raise
        # A connection cleanup error cannot justify another paid request or
        # replace a durable accounting/cancellation failure already in flight.
        logger.warning("LLM connection cleanup failed (%s)", type(exc).__name__)


async def governed_request(operation_factory, *, model, base_url, messages, stage=None,
                           cancel_check=None, timeout=None, usage_observer=None,
                           before_dispatch=None, on_lease=None, limiter=None,
                           route_health=None, max_output_tokens=None):
    route, estimated = _guards(model, base_url, messages, cancel_check, timeout, max_output_tokens)
    owns_limiter, owns_health = limiter is None, route_health is None
    limiter = limiter if limiter is not None else DistributedLLMTokenBucket()
    health = route_health if route_health is not None else DistributedRouteHealth()
    lease, state, primary = None, {}, None
    started = None
    try:
        lease = await limiter.acquire(provider=route[0], model=route[1],
                                      estimated_tokens=estimated, cancel_check=cancel_check)
        _before(estimated, model=model, base_url=base_url, messages=messages, stage=stage,
                max_output_tokens=max_output_tokens, cancel_check=cancel_check,
                before_dispatch=before_dispatch, on_lease=on_lease, lease=lease)
        async def physical():
            nonlocal started
            raise_if_cancelled(cancel_check)
            started = time.monotonic()
            try:
                state["response"] = await operation_factory()
                return state["response"]
            except BaseException as exc:
                if _transport_error(exc):
                    state["transport_error"] = exc
                raise
        operation = accounted_request(physical(), model=model, base_url=base_url,
                                      stage=stage, usage_observer=usage_observer)
        if timeout is not None or cancel_check is not None:
            return await bounded_request(operation, timeout=timeout or 3600, cancel_check=cancel_check)
        return await operation
    except BaseException as exc:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) and started is not None and "response" not in state:
            state["transport_error"] = exc
        primary = exc
        raise
    finally:
        try:
            if started is not None:
                _health(health, route, state, int((time.monotonic() - started) * 1000))
            usage = normalize_usage(state.get("response"))
            if lease is not None and usage["usage_status"] == "complete":
                await limiter.reconcile(lease, actual_tokens=usage["total_tokens"])
        except BaseException as exc:
            if primary is None:
                raise
            logger.warning("LLM completion guard also failed (%s); preserving original failure", type(exc).__name__)
        finally:
            active_failure = primary or sys.exc_info()[1]
            if owns_limiter:
                _close_owned(limiter, active_failure)
            if owns_health:
                _close_owned(health, active_failure)


def governed_call(operation_factory, *, model, base_url, messages, stage=None,
                  cancel_check=None, timeout=None, usage_observer=None,
                  before_dispatch=None, on_lease=None, limiter=None,
                  route_health=None, response_usage=lambda value: value,
                  max_output_tokens=None):
    """Synchronous providers retain their socket deadline; never detach a call.

Once a sync call has started it cannot be safely killed by an async timeout.
Cancellation/deadline after return retains its accounting and rejects delivery.
"""
    route, estimated = _guards(model, base_url, messages, cancel_check, timeout, max_output_tokens)
    owns_limiter, owns_health = limiter is None, route_health is None
    limiter = limiter if limiter is not None else DistributedLLMTokenBucket()
    health = route_health if route_health is not None else DistributedRouteHealth()
    lease, state, primary = None, {}, None
    started = None
    try:
        lease = limiter.acquire_sync(provider=route[0], model=route[1],
                                     estimated_tokens=estimated, cancel_check=cancel_check)
        _before(estimated, model=model, base_url=base_url, messages=messages, stage=stage,
                max_output_tokens=max_output_tokens, cancel_check=cancel_check,
                before_dispatch=before_dispatch, on_lease=on_lease, lease=lease)
        def physical():
            nonlocal started
            raise_if_cancelled(cancel_check)
            started = time.monotonic()
            try:
                response = operation_factory()
            except BaseException as exc:
                if _transport_error(exc):
                    state["transport_error"] = exc
                raise
            try:
                state["response"] = response_usage(response)
            except Exception:
                state["response"] = None
            return response
        response = accounted_call(physical, model=model, base_url=base_url, stage=stage,
                                  response_usage=lambda value: state.get("response"))
        if usage_observer:
            usage_observer(normalize_usage(state.get("response")))
        raise_if_cancelled(cancel_check)
        if timeout is not None and time.monotonic() - started > timeout:
            raise TimeoutError("LLM synchronous request deadline exceeded")
        return response
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            if started is not None:
                _health(health, route, state, int((time.monotonic() - started) * 1000))
            usage = normalize_usage(state.get("response"))
            if lease is not None and usage["usage_status"] == "complete":
                limiter.reconcile_sync(lease, actual_tokens=usage["total_tokens"])
        except BaseException as exc:
            if primary is None:
                raise
            logger.warning("LLM completion guard also failed (%s); preserving original failure", type(exc).__name__)
        finally:
            active_failure = primary or sys.exc_info()[1]
            if owns_limiter:
                _close_owned(limiter, active_failure)
            if owns_health:
                _close_owned(health, active_failure)
