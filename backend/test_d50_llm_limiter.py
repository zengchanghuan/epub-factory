"""Offline Redis protocol-model tests. No real Redis server or provider used.

The locked fake models atomic script state, TTL/generation and replies, but is
not a Lua interpreter; production Redis integration remains a separate check.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from app.cancellation import JobCancelled
from app.infra.llm_token_bucket import (
    DistributedLLMTokenBucket, GatewayConfigurationError, GatewayRequestTooLarge,
    GatewayUnavailable, GatewayWaitTimeout, _ACQUIRE_LUA, _ADJUST_LUA,
)
from app.infra.llm_route_health import DistributedRouteHealth, _UPDATE_LUA

MODEL = "deepseek-flash"


class FakeRedis:
    def __init__(self):
        self.lock = threading.RLock()
        self.hashes, self.expiries, self.calls = {}, {}, []
        self.now = 0
        self.failure = None
        self.closed = False

    def _expire(self):
        for key, expiry in list(self.expiries.items()):
            if expiry <= self.now:
                self.hashes.pop(key, None); self.expiries.pop(key, None)

    def eval(self, script, numkeys, *args):
        with self.lock:
            self.calls.append((script, numkeys, args))
            if self.failure: raise self.failure
            self._expire()
            if script == _ACQUIRE_LUA:
                request_key, token_key, lease_key, rpm, tpm, cost, ttl, identity = args
                assert numkeys == 3
                if lease_key in self.hashes: return [1, 0]
                def refill(key, capacity):
                    row = self.hashes.get(key, {})
                    return min(capacity, row.get("tokens", capacity) + max(0, self.now-row.get("updated", self.now))*capacity/60000)
                req, tokens = refill(request_key, rpm), refill(token_key, tpm)
                generation = self.hashes.get(token_key, {}).get("generation", identity)
                allowed = req >= 1 and tokens >= cost
                if allowed:
                    req -= 1; tokens -= cost
                    self.hashes[lease_key] = {"estimated": cost, "settled": 0, "generation": generation}
                    self.expiries[lease_key] = self.now+ttl
                self.hashes[request_key] = {"tokens": req, "updated": self.now}
                self.hashes[token_key] = {"tokens": tokens, "updated": self.now, "generation": generation}
                self.expiries[request_key] = self.expiries[token_key] = self.now+ttl
                return [int(allowed), 0 if allowed else max(max(0, 1-req)*60000/rpm, max(0, cost-tokens)*60000/tpm)]
            if script == _ADJUST_LUA:
                token_key, lease_key, capacity, actual = args
                assert numkeys == 2
                lease = self.hashes.get(lease_key)
                if not lease or lease["settled"]: return 0
                lease["settled"] = 1
                token = self.hashes.get(token_key)
                if not token or token["generation"] != lease["generation"]: return 0
                token["tokens"] = min(capacity, token["tokens"] + lease["estimated"] - actual)
                return 1
            if script == _UPDATE_LUA:
                key, mode, latency, ttl = args
                assert numkeys == 1
                row = self.hashes.setdefault(key, {})
                failures = row.get("failures", 0)
                if mode == "failure":
                    failures = min(10, failures+1)
                    row.update(failures=failures, cooldown_until_epoch=self.now/1000+min(60, failures*5))
                else:
                    row.update(failures=max(0, failures-1), cooldown_until_epoch=0,
                               latency_ms=row.get("latency_ms", latency)*.7+latency*.3)
                self.expiries[key] = self.now+ttl*1000
                return 1
            raise AssertionError("unexpected Lua script")

    def pipeline(self, **kwargs):
        outer = self
        class Pipeline:
            def __init__(self): self.keys = []
            def hgetall(self, key): self.keys.append(key)
            def execute(self):
                if outer.failure: raise outer.failure
                return [dict(outer.hashes.get(key, {})) for key in self.keys]
        return Pipeline()

    def close(self): self.closed = True


class LimiterTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"EPUB_LLM_RATE_LIMITER_ENABLED": "1", "EPUB_LLM_RPM": "5",
            "EPUB_LLM_TPM": "1000", "REDIS_URL": "redis://offline.invalid/0",
            "EPUB_LLM_RATE_LIMIT_MAX_WAIT": "0.02", "EPUB_LLM_RATE_LIMIT_FAIL_OPEN": "0",
            "EPUB_LLM_GLOBAL_HEALTH_ENABLED": "1"})
        env.start(); self.addCleanup(env.stop)
        for target in ("socket.socket.connect", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            mock = guard.start(); self.addCleanup(guard.stop); self.addCleanup(mock.assert_not_called)
        self.redis = FakeRedis()

    def limiter(self):
        limiter = DistributedLLMTokenBucket(); limiter._redis = self.redis
        return limiter

    @staticmethod
    def args(**kwargs):
        return dict(provider="api.deepseek.com", model=MODEL, estimated_tokens=40, **kwargs)

    def test_disabled_gate_does_not_construct_redis(self):
        with patch.dict(os.environ, {"EPUB_LLM_RATE_LIMITER_ENABLED": "0", "EPUB_LLM_RPM": "invalid"}):
            limiter = DistributedLLMTokenBucket()
        self.assertFalse(limiter.acquire_sync(**self.args()).enabled)
        self.assertIsNone(limiter._redis)

    def test_enabled_incomplete_invalid_and_nonfinite_configuration_fail_closed(self):
        for fields in ({"EPUB_LLM_RPM": "0"}, {"EPUB_LLM_TPM": "-1"}, {"EPUB_LLM_RPM": "1.5"},
                       {"EPUB_LLM_RATE_LIMIT_MAX_WAIT": "nan"}, {"EPUB_LLM_RATE_LIMIT_MAX_WAIT": "inf"},
                       {"REDIS_URL": "", "CELERY_BROKER_URL": ""}, {"REDIS_URL": "amqp://offline"}):
            with patch.dict(os.environ, fields): limiter = DistributedLLMTokenBucket()
            with self.assertRaises(GatewayConfigurationError): limiter.acquire_sync(**self.args())
            self.assertIsNone(limiter._redis)

    def test_oversize_request_rejected_not_clipped_or_sent_to_redis(self):
        limiter = self.limiter()
        for cost in (1001, 10**20):
            with self.assertRaises(GatewayRequestTooLarge):
                limiter.acquire_sync(provider="api.deepseek.com", model=MODEL, estimated_tokens=cost)
        self.assertEqual(self.redis.calls, [])

    def test_invalid_estimates_rejected_without_redis(self):
        for cost in (True, -1, 0, 2.5, "20"):
            with self.assertRaises(GatewayConfigurationError):
                self.limiter().acquire_sync(provider="api.deepseek.com", model=MODEL, estimated_tokens=cost)
        self.assertEqual(self.redis.calls, [])

    def test_explicit_fail_open_recovers_next_request_not_permanent_disable(self):
        limiter = self.limiter(); limiter.fail_open = True
        self.redis.failure = RuntimeError("secret credential must not be logged")
        with self.assertLogs("epub_factory.llm_token_bucket", level="WARNING") as logs:
            lease = limiter.acquire_sync(**self.args())
        self.assertTrue(lease.bypassed); self.assertTrue(limiter.enabled)
        self.assertNotIn("secret credential", repr(logs.output))
        self.redis.failure = None
        self.assertTrue(limiter.acquire_sync(**self.args()).enabled)

    def test_default_redis_failure_fails_closed_and_recovers_next_request(self):
        limiter = self.limiter(); self.redis.failure = ConnectionError("offline")
        with self.assertRaises(GatewayUnavailable): asyncio.run(limiter.acquire(**self.args()))
        self.assertTrue(limiter.enabled)
        self.redis.failure = None
        self.assertTrue(asyncio.run(limiter.acquire(**self.args())).enabled)

    def test_two_async_loops_and_sync_share_bucket_aliases(self):
        limiter = self.limiter()
        first = asyncio.run(limiter.acquire(**self.args()))
        second = asyncio.run(limiter.acquire(provider="https://API.DeepSeek.com/v1", model="deepseek-v4-flash", estimated_tokens=40))
        third = limiter.acquire_sync(provider="api.deepseek.com", model="deepseek-v4-flash-vision-exp", estimated_tokens=40)
        self.assertEqual({first.token_key, second.token_key, third.token_key}, {first.token_key})
        self.assertEqual(self.redis.hashes[first.token_key]["tokens"], 880)
        self.assertEqual(len({first.reservation_key, second.reservation_key, third.reservation_key}), 3)

    def test_concurrent_sync_async_workers_share_exact_rpm_capacity(self):
        def request(index):
            limiter = self.limiter()
            try:
                if index % 2: asyncio.run(limiter.acquire(**self.args()))
                else: limiter.acquire_sync(**self.args())
                return True
            except GatewayWaitTimeout: return False
        with ThreadPoolExecutor(max_workers=12) as pool: outcomes = list(pool.map(request, range(40)))
        self.assertEqual(sum(outcomes), 5)

    def test_tpm_refund_idempotent_debt_and_unknown_usage(self):
        limiter = self.limiter(); limiter.tpm = 100
        first = limiter.acquire_sync(**self.args()); limiter.acquire_sync(**self.args())
        with self.assertRaises(GatewayWaitTimeout): limiter.acquire_sync(**self.args())
        limiter.reconcile_sync(first, actual_tokens=10)
        self.assertEqual(self.redis.hashes[first.token_key]["tokens"], 50)
        limiter.reconcile_sync(first, actual_tokens=10)
        self.assertEqual(self.redis.hashes[first.token_key]["tokens"], 50)
        third = limiter.acquire_sync(**self.args())
        limiter.reconcile_sync(third, actual_tokens=200)
        self.assertEqual(self.redis.hashes[first.token_key]["tokens"], -150)
        limiter.reconcile_sync(third, actual_tokens=None)
        self.assertEqual(self.redis.hashes[first.token_key]["tokens"], -150)

    def test_acquire_lua_replay_is_idempotent(self):
        limiter = self.limiter(); lease = limiter.acquire_sync(**self.args())
        script, count, args = self.redis.calls[-1]
        self.assertEqual(self.redis.eval(script, count, *args), [1, 0])
        self.assertEqual(self.redis.hashes[lease.token_key]["tokens"], 960)

    def test_expired_old_lease_cannot_refund_new_generation(self):
        limiter = self.limiter(); old = limiter.acquire_sync(**self.args())
        self.redis.hashes.pop(old.token_key)
        fresh = limiter.acquire_sync(**self.args())
        before = self.redis.hashes[fresh.token_key]["tokens"]
        limiter.reconcile_sync(old, actual_tokens=0)
        self.assertEqual(self.redis.hashes[fresh.token_key]["tokens"], before)

    def test_atomic_refill_uses_shared_clock_not_client_clock(self):
        limiter = self.limiter()
        for _ in range(5): limiter.acquire_sync(**self.args())
        with self.assertRaises(GatewayWaitTimeout): limiter.acquire_sync(**self.args())
        self.redis.now += 12000
        self.assertTrue(limiter.acquire_sync(**self.args()).enabled)
        with self.assertRaises(GatewayWaitTimeout): limiter.acquire_sync(**self.args())
        self.assertIn("redis.call('TIME')", _ACQUIRE_LUA)

    def test_cancellation_interrupts_wait_without_permanent_disable(self):
        limiter = self.limiter(); limiter.max_wait_seconds = 1
        for _ in range(5): limiter.acquire_sync(**self.args())
        async def run():
            cancelled = False
            task = asyncio.create_task(limiter.acquire(**self.args(cancel_check=lambda: cancelled)))
            await asyncio.sleep(.02); cancelled = True
            with self.assertRaises(JobCancelled): await task
        asyncio.run(run()); self.assertTrue(limiter.enabled)

    def test_soft_time_limit_never_becomes_fail_open(self):
        from billiard.exceptions import SoftTimeLimitExceeded
        limiter = self.limiter(); limiter.fail_open = True; self.redis.failure = SoftTimeLimitExceeded()
        with self.assertRaises(SoftTimeLimitExceeded): limiter.acquire_sync(**self.args())
        with self.assertRaises(SoftTimeLimitExceeded): asyncio.run(limiter.acquire(**self.args()))
        self.assertTrue(limiter.enabled)

    def test_cancelled_redis_thread_closes_only_after_actual_io_returns(self):
        entered, release = threading.Event(), threading.Event()
        limiter = self.limiter(); original = self.redis.eval
        def slow(*args):
            entered.set(); release.wait(2)
            self.assertFalse(self.redis.closed)
            return original(*args)
        self.redis.eval = slow
        async def run():
            task = asyncio.create_task(limiter.acquire(**self.args()))
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set()); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            limiter.close(); self.assertFalse(self.redis.closed)
            release.set()
            for _ in range(100):
                if self.redis.closed: break
                await asyncio.sleep(.005)
            self.assertTrue(self.redis.closed)
        try: asyncio.run(run())
        finally: release.set()

    def test_reconcile_failure_fail_closed_or_explicit_open(self):
        limiter = self.limiter(); lease = limiter.acquire_sync(**self.args())
        self.redis.failure = ConnectionError("offline")
        with self.assertRaises(GatewayUnavailable): limiter.reconcile_sync(lease, actual_tokens=10)
        limiter.fail_open = True; limiter.reconcile_sync(lease, actual_tokens=10)
        self.redis.failure = None; asyncio.run(limiter.reconcile(lease, actual_tokens=10))
        self.assertEqual(self.redis.hashes[lease.token_key]["tokens"], 990)

    def test_health_updates_atomic_alias_shared_and_transient_errors_recover(self):
        with patch("redis.Redis.from_url", return_value=self.redis): health = DistributedRouteHealth()
        a, b = ("https://api.deepseek.com/v1", MODEL), ("https://api.deepseek.com", "deepseek-v4-flash")
        with ThreadPoolExecutor(max_workers=6) as pool: list(pool.map(lambda _: health.record_failure(a), range(30)))
        self.assertEqual(health.snapshot([b])[b]["failures"], 10)
        health.record_success(b, 100)
        self.assertEqual(health.snapshot([a])[a]["failures"], 9)
        self.redis.failure = RuntimeError("offline")
        self.assertFalse(health.record_success(a, 20)); self.assertEqual(health.snapshot([a]), {})
        self.assertTrue(health.enabled)
        self.redis.failure = None
        self.assertTrue(health.record_success(a, 20))

    def test_five_physical_stages_share_one_rpm_and_sixth_never_dispatches(self):
        from sqlalchemy import create_engine
        from app.infra.llm_gateway import governed_request, governed_call
        from app.infra.llm_usage_ledger import get_ledger, usage_scope

        stages = ("book_profile", "glossary", "body", "failed_chunk_rescue", "precision_polish")
        accepted, release = threading.Event(), threading.Event()
        counter_lock = threading.Lock()
        calls = []
        response = {"model": MODEL, "usage": {"prompt_tokens": 10, "completion_tokens": 3,
            "total_tokens": 13, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "EPUB_LLM_TPM": "10000", "EPUB_LLM_GLOBAL_HEALTH_ENABLED": "0"}):
            engine = create_engine("sqlite:///" + str(Path(directory) / "ledger.db"),
                                   connect_args={"check_same_thread": False})
            try:
                ledger = get_ledger(engine)
                def entered(stage):
                    with counter_lock:
                        calls.append(stage)
                        if len(calls) == 5:
                            accepted.set()

                def work(stage):
                    limiter = self.limiter()  # Real limiter; only Redis.eval is a protocol model.
                    metadata = dict(model=MODEL, base_url="https://api.deepseek.com/v1",
                        messages=[{"role": "user", "content": "Historical bounded sample"}],
                        stage=stage, limiter=limiter)
                    with usage_scope(stage, "attempt", engine=engine):
                        if stage == "precision_polish":
                            def physical():
                                entered(stage)
                                if not release.wait(5): raise AssertionError("concurrent test timed out")
                                return response
                            return governed_call(physical, **metadata)
                        async def physical():
                            entered(stage)
                            if not await asyncio.to_thread(release.wait, 5):
                                raise AssertionError("concurrent test timed out")
                            return response
                        return asyncio.run(governed_request(physical, **metadata))

                with ThreadPoolExecutor(max_workers=5) as pool:
                    futures = [pool.submit(work, stage) for stage in stages]
                    try:
                        self.assertTrue(accepted.wait(4), "all five stages must be simultaneously in flight")
                        denied = AsyncMock(return_value=response)
                        with usage_scope("sixth", "attempt", engine=engine), self.assertRaises(GatewayWaitTimeout):
                            asyncio.run(governed_request(denied, model="deepseek-v4-flash",
                                base_url="https://api.deepseek.com", messages=[{"role": "user", "content": "extra"}],
                                stage="body", limiter=self.limiter()))
                        denied.assert_not_called()
                        self.assertEqual(ledger.requests("sixth"), [])
                        self.assertCountEqual(calls, stages)
                        for stage in stages:
                            rows = ledger.requests(stage)
                            self.assertEqual(len(rows), 1)
                            self.assertEqual(rows[0]["request_status"], "in_flight")
                    finally:
                        release.set()
                    for future in futures: self.assertEqual(future.result(timeout=5), response)
                for stage in stages:
                    row = ledger.requests(stage)[0]
                    self.assertEqual(row["request_status"], "response")
                    self.assertEqual(row["stage"], stage)
                    self.assertEqual(row["total_tokens"], 13)
                request_keys = [key for key in self.redis.hashes if key.endswith(":requests")]
                self.assertEqual(len(request_keys), 1)
                self.assertEqual(self.redis.hashes[request_keys[0]]["tokens"], 0)
            finally:
                release.set()
                engine.dispose()


if __name__ == "__main__": unittest.main()
