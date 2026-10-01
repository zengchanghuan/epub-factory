"""Offline physical-request boundary tests: real SQLite ledger, no provider I/O."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import create_engine
from app.cancellation import JobCancelled
from app.infra.llm_gateway import (
    GatewayControlError, governed_request, governed_call, dispatch_budget_scope,
    preflight_output_limit,
)
from app.infra.llm_guard import ModelNotAllowedError
from app.infra.llm_token_bucket import TokenBucketLease, GatewayUnavailable
from app.infra.llm_usage_ledger import usage_scope, get_ledger, AccountingError

MODEL, HOST = "deepseek-flash", "https://api.deepseek.com/v1"
MESSAGES = [{"role": "user", "content": "Source sample"}]


def response(total=13):
    return {"id": None, "model": MODEL, "usage": {
        "prompt_tokens": 10, "completion_tokens": total - 10, "total_tokens": total,
        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10},
        "choices": [{"message": {"content": "not semantically interpreted by gateway"}}]}


class Limiter:
    def __init__(self):
        self.requests, self.adjustments = [], []
        self.failure = None
    def acquire_sync(self, **kwargs):
        if self.failure:
            raise self.failure
        self.requests.append(kwargs)
        return TokenBucketLease(kwargs["estimated_tokens"], 7, True, "tokens", "lease")
    async def acquire(self, **kwargs):
        return self.acquire_sync(**kwargs)
    def reconcile_sync(self, lease, **kwargs):
        self.adjustments.append(kwargs["actual_tokens"])
    async def reconcile(self, lease, **kwargs):
        self.reconcile_sync(lease, **kwargs)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "ledger.db"),
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        self.ledger = get_ledger(self.engine)
        self.limiter = Limiter()
        self.health = N(record_success=Mock(return_value=True), record_failure=Mock(return_value=True))
        self.env = patch.dict(os.environ, {"LLM_MODEL_ALLOWLIST": "deepseek-flash"})
        self.env.start(); self.addCleanup(self.env.stop)
        for target in ("socket.socket.connect", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            mock = guard.start(); self.addCleanup(guard.stop); self.addCleanup(mock.assert_not_called)

    def arguments(self, **overrides):
        return dict(model=MODEL, base_url=HOST, messages=MESSAGES, limiter=self.limiter,
                    route_health=self.health, **overrides)

    def run_async(self, factory=None, **kwargs):
        factory = factory or AsyncMock(return_value=response())
        with usage_scope("book", "attempt", engine=self.engine):
            return asyncio.run(governed_request(factory, **self.arguments(**kwargs)))

    def test_async_guard_order_and_one_request_accounted(self):
        events = []
        async def request():
            events.append("factory"); return response()
        def reserve(estimate, **metadata):
            self.assertGreater(estimate, 0); self.assertEqual(metadata["stage"], "book_profile")
            self.assertEqual(metadata["max_output_tokens"], 4096); events.append("budget")
        with dispatch_budget_scope(reserve):
            self.run_async(request, stage="book_profile", max_output_tokens=4096,
                on_lease=lambda lease: events.append("lease"), before_dispatch=lambda: events.append("checkpoint"))
        self.assertEqual(events, ["lease", "budget", "checkpoint", "factory"])
        self.assertEqual(len(self.ledger.requests("book")), 1)
        self.assertEqual(self.limiter.adjustments, [13])
        self.health.record_success.assert_called_once(); self.health.record_failure.assert_not_called()

    def test_sync_and_async_stages_share_same_route_identity(self):
        for stage in ("body", "glossary", "book_profile", "precision_polish"):
            with usage_scope("book", "attempt", engine=self.engine):
                if stage == "precision_polish":
                    governed_call(lambda: response(), **self.arguments(stage=stage))
                else:
                    asyncio.run(governed_request(AsyncMock(return_value=response()), **self.arguments(stage=stage)))
        self.assertEqual({(r["provider"], r["model"]) for r in self.limiter.requests}, {("api.deepseek.com", MODEL)})
        self.assertEqual({r["stage"] for r in self.ledger.requests("book")}, {"body", "glossary", "book_profile", "precision_polish"})

    def test_alias_bucket_but_actual_model_retained_in_ledger(self):
        with usage_scope("book", "attempt", engine=self.engine):
            asyncio.run(governed_request(AsyncMock(return_value=response()), model="deepseek-v4-flash",
                base_url="https://user:secret@API.DeepSeek.com/v1?secret=x", messages=MESSAGES,
                limiter=self.limiter, route_health=self.health))
        self.assertEqual(self.limiter.requests[0]["model"], MODEL)
        row = self.ledger.requests("book")[0]
        self.assertEqual(row["requested_model"], "deepseek-v4-flash")
        self.assertEqual(row["provider"], "api.deepseek.com")
        self.assertNotIn("secret", repr(row))

    def test_control_and_budget_rejections_create_no_request(self):
        for origin in ("quota", "budget", "checkpoint"):
            factory = AsyncMock(return_value=response())
            def reject(*args, **kwargs): raise GatewayControlError("bounded refusal")
            self.limiter.failure = GatewayUnavailable("offline") if origin == "quota" else None
            with dispatch_budget_scope(reject if origin == "budget" else None):
                with self.assertRaises(GatewayControlError):
                    self.run_async(factory, before_dispatch=reject if origin == "checkpoint" else None)
            factory.assert_not_called()
        self.assertEqual(self.ledger.requests("book"), [])

    def test_allowlist_checked_before_quota_and_factory(self):
        factory = AsyncMock()
        with self.assertRaises(ModelNotAllowedError):
            asyncio.run(governed_request(factory, model="expensive-unknown", base_url=HOST,
                messages=MESSAGES, limiter=self.limiter, route_health=self.health))
        factory.assert_not_called(); self.assertEqual(self.limiter.requests, [])

    def test_each_compatibility_retry_has_its_own_reservation_and_account(self):
        failed, successful = AsyncMock(side_effect=ValueError("response_format unsupported")), AsyncMock(return_value=response())
        with self.assertRaises(ValueError): self.run_async(failed)
        self.run_async(successful)
        self.assertEqual(len(self.limiter.requests), 2)
        self.assertEqual(self.limiter.adjustments, [13])
        self.assertEqual(len(self.ledger.requests("book")), 2)
        self.health.record_failure.assert_called_once()

    def test_json_parse_failure_is_outside_gateway_but_usage_reconciled(self):
        self.run_async()
        self.assertEqual(self.limiter.adjustments, [13])
        self.assertEqual(self.ledger.requests("book")[0]["request_status"], "response")

    def test_unknown_or_invalid_usage_never_refunds_reservation(self):
        for value in ({"choices": []}, response(total=9)):
            self.run_async(AsyncMock(return_value=value))
        self.assertEqual(self.limiter.adjustments, [])
        self.assertEqual(len(self.ledger.requests("book")), 2)

    def test_accounting_begin_failure_zero_factory(self):
        factory = AsyncMock(return_value=response())
        with patch.object(self.ledger, "begin", side_effect=RuntimeError("db unavailable")):
            with self.assertRaises(AccountingError): self.run_async(factory)
        factory.assert_not_called(); self.assertEqual(self.ledger.requests("book"), [])

    def test_accounting_finish_failure_no_repeat_and_known_usage_reconciled(self):
        factory = AsyncMock(return_value=response())
        with patch.object(self.ledger, "finish", side_effect=RuntimeError("db unavailable")):
            with self.assertRaises(AccountingError): self.run_async(factory)
        self.assertEqual(factory.call_count, 1); self.assertEqual(self.limiter.adjustments, [13])
        self.assertEqual(self.ledger.requests("book")[0]["request_status"], "in_flight")

    def test_cancel_before_dispatch_has_no_factory_or_ledger_request(self):
        factory = AsyncMock()
        with self.assertRaises(JobCancelled): self.run_async(factory, cancel_check=lambda: True)
        factory.assert_not_called(); self.assertEqual(self.ledger.requests("book"), [])

    def test_cancel_after_response_preserves_actual_usage_and_rejects_delivery(self):
        cancelled = False
        async def request():
            nonlocal cancelled
            cancelled = True; return response()
        with self.assertRaises(JobCancelled): self.run_async(request, cancel_check=lambda: cancelled)
        self.assertEqual(self.limiter.adjustments, [13])
        self.assertEqual(self.ledger.requests("book")[0]["request_status"], "response")

    def test_absolute_timeout_cancels_request_no_refund(self):
        cancelled = []
        async def request():
            try: await asyncio.sleep(20)
            finally: cancelled.append(True)
        with self.assertRaises(asyncio.TimeoutError): self.run_async(request, timeout=.01)
        self.assertEqual(cancelled, [True]); self.assertEqual(self.limiter.adjustments, [])
        self.assertEqual(self.ledger.requests("book")[0]["request_status"], "cancelled")
        self.health.record_failure.assert_called_once()

    def test_soft_time_limit_not_health_failure_and_not_swallowed(self):
        from billiard.exceptions import SoftTimeLimitExceeded
        with self.assertRaises(SoftTimeLimitExceeded): self.run_async(AsyncMock(side_effect=SoftTimeLimitExceeded()))
        self.health.record_failure.assert_not_called(); self.assertEqual(self.limiter.adjustments, [])

    def test_health_failure_advisory_does_not_invalidate_accounted_response(self):
        self.health.record_success.side_effect = RuntimeError("health offline")
        self.assertEqual(self.run_async()["usage"]["total_tokens"], 13)
        self.assertEqual(self.limiter.adjustments, [13])

    def test_sync_response_adapter_runs_once_and_cancellation_stays_accounted(self):
        cancelled = False
        def operation():
            nonlocal cancelled
            cancelled = True; return N(data=response())
        adapter = Mock(side_effect=lambda item: item.data)
        with usage_scope("book", "attempt", engine=self.engine), self.assertRaises(JobCancelled):
            governed_call(operation, **self.arguments(cancel_check=lambda: cancelled), response_usage=adapter)
        adapter.assert_called_once(); self.assertEqual(self.limiter.adjustments, [13])
        self.assertEqual(self.ledger.requests("book")[0]["request_status"], "response")

    def test_parallel_context_budgets_isolated_and_reset(self):
        seen = []
        def work(book):
            with usage_scope(book, "preflight", engine=self.engine), dispatch_budget_scope(lambda estimate, **kwargs: seen.append(book)):
                asyncio.run(governed_request(AsyncMock(return_value=response()), **self.arguments()))
        with ThreadPoolExecutor(max_workers=3) as pool: list(pool.map(work, ("a", "b", "c")))
        self.assertCountEqual(seen, ("a", "b", "c"))
        self.run_async(); self.assertEqual(len(seen), 3)
        for book in ("a", "b", "c"): self.assertEqual(len(self.ledger.requests(book)), 1)

    def test_output_limit_is_bounded_and_invalid_config_fails_closed(self):
        for raw in ("0", "16385", "nan", "1.5"):
            with patch.dict(os.environ, {"EPUB_PREFLIGHT_MAX_OUTPUT_TOKENS": raw}), self.assertRaises(GatewayControlError):
                preflight_output_limit()
        with patch.dict(os.environ, {"EPUB_PREFLIGHT_MAX_OUTPUT_TOKENS": "4096"}):
            self.assertEqual(preflight_output_limit(), 4096)

    def test_owned_close_failure_cannot_trigger_replay_of_successful_request(self):
        self.limiter.close = Mock(side_effect=RuntimeError("cleanup failure"))
        self.health.close = Mock(side_effect=RuntimeError("cleanup failure"))
        with patch("app.infra.llm_gateway.DistributedLLMTokenBucket", return_value=self.limiter), \
                patch("app.infra.llm_gateway.DistributedRouteHealth", return_value=self.health), \
                usage_scope("book", "attempt", engine=self.engine):
            factory = AsyncMock(return_value=response())
            result = asyncio.run(governed_request(factory, model=MODEL, base_url=HOST, messages=MESSAGES))
            self.assertEqual(result["usage"]["total_tokens"], 13)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(governed_call(lambda: response(), model=MODEL, base_url=HOST, messages=MESSAGES)["usage"]["total_tokens"], 13)
        self.assertEqual(len(self.ledger.requests("book")), 2)

    def test_close_failure_preserves_accounting_failure_and_control_propagates(self):
        from billiard.exceptions import SoftTimeLimitExceeded
        self.limiter.close = Mock(side_effect=RuntimeError("cleanup failure"))
        self.health.close = Mock()
        with patch("app.infra.llm_gateway.DistributedLLMTokenBucket", return_value=self.limiter), \
                patch("app.infra.llm_gateway.DistributedRouteHealth", return_value=self.health), \
                usage_scope("book", "attempt", engine=self.engine):
            with patch.object(self.ledger, "finish", side_effect=RuntimeError("db")), self.assertRaises(AccountingError):
                asyncio.run(governed_request(AsyncMock(return_value=response()), model=MODEL, base_url=HOST, messages=MESSAGES))
            self.limiter.close.side_effect = SoftTimeLimitExceeded()
            with self.assertRaises(SoftTimeLimitExceeded):
                governed_call(lambda: response(), model=MODEL, base_url=HOST, messages=MESSAGES)


if __name__ == "__main__": unittest.main()
