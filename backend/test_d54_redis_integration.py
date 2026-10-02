"""Opt-in, real Redis acceptance for the production LLM quota Lua.

The operator must start a disposable, loopback-only Redis on a non-default
port. This test never starts Redis, contacts providers, flushes a database, or
loads the application's .env. Only this run's random keys are deleted.

Run from the repository root (no all-network-denying sitecustomize guard):
    D54_REDIS_ISOLATED=1 D54_REDIS_URL=redis://127.0.0.1:16379/0 \
      PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -u \
      backend/test_d54_redis_integration.py -v

The built-in socket guard permits only the supplied Redis endpoint and an
ephemeral loopback fault proxy. Missing opt-in exits 2 when run as a script;
unittest discovery reports a skip, never acceptance. Invalid/unavailable
explicit opt-in fails. Provider responses are controlled, not model quality
evidence. The outage test cuts the proxy, not the operator's Redis server.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import ipaddress
import multiprocessing
import os
from pathlib import Path
import queue
import select
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit, urlunsplit
import uuid

# Importing app.infra also initializes the Celery application. Do not read real
# broker/provider credentials from a developer .env, including in spawn children.
with patch("dotenv.load_dotenv", return_value=False):
    from app.infra.llm_token_bucket import (
        DistributedLLMTokenBucket, GatewayRequestTooLarge, GatewayUnavailable,
        GatewayWaitTimeout, _ACQUIRE_LUA, _ADJUST_LUA,
    )
    from app.infra.llm_gateway import governed_call, governed_request
    from app.infra.llm_route_health import DistributedRouteHealth
    from app.infra.llm_usage_ledger import get_ledger, usage_scope


MODEL = "deepseek-flash"
ALIASES = (MODEL, "deepseek-v4-flash", "deepseek-v4-flash-vision-exp")
HOST = "https://api.deepseek.com/v1"
MESSAGES = [{"role": "user", "content": "Offline infrastructure acceptance sample"}]


def _validated_url(value, isolated):
    """No fallback to REDIS_URL, credentials, DNS, default port, or URL options."""
    try:
        parts = urlsplit(value)
        if isolated != "1" or parts.scheme != "redis":
            raise ValueError
        if parts.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError
        if parts.port is None or parts.port == 6379 or not 1 <= parts.port <= 65535:
            raise ValueError
        if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise ValueError
        if not parts.path.startswith("/") or not parts.path[1:].isdigit():
            raise ValueError
        if not 0 <= int(parts.path[1:]) <= 15:
            raise ValueError
        return value, (parts.hostname, parts.port)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("D54 requires an explicitly acknowledged isolated redis://127.0.0.1:<non-6379-port>/<db> (or ::1), without credentials or URL options") from None


class _LoopbackGuard:
    """Allow exact local endpoints only, including in independently spawned clients."""

    def __init__(self, endpoint):
        self.allowed = {endpoint}
        self.blocked = []
        self.stack = ExitStack()

    def __enter__(self):
        original_resolve = socket.getaddrinfo
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def check(host, port):
            try:
                target = (str(ipaddress.ip_address(host)), int(port))
            except (TypeError, ValueError):
                target = None
            if target not in self.allowed:
                self.blocked.append("non-approved socket or DNS attempt")
                raise AssertionError("D54 forbids non-approved network destinations")

        def resolve(host, port, *args, **kwargs):
            check(host, port)
            return original_resolve(host, port, *args, **kwargs)

        def connect(sock, address):
            if not isinstance(address, tuple) or len(address) < 2:
                check(None, None)
            check(address[0], address[1])
            return original_connect(sock, address)

        def connect_ex(sock, address):
            if not isinstance(address, tuple) or len(address) < 2:
                check(None, None)
            check(address[0], address[1])
            return original_connect_ex(sock, address)

        self.stack.enter_context(patch("socket.getaddrinfo", resolve))
        self.stack.enter_context(patch("socket.socket.connect", connect))
        self.stack.enter_context(patch("socket.socket.connect_ex", connect_ex))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


def _environment(url, prefix, *, rpm=5, tpm=1000):
    return {
        "REDIS_URL": url, "CELERY_BROKER_URL": "", "LLM_MODEL_ALLOWLIST": MODEL,
        "EPUB_LLM_RATE_LIMITER_ENABLED": "1", "EPUB_LLM_RATE_LIMIT_FAIL_OPEN": "0",
        "EPUB_LLM_RATE_LIMIT_KEY_PREFIX": prefix, "EPUB_LLM_RPM": str(rpm),
        "EPUB_LLM_TPM": str(tpm), "EPUB_LLM_RATE_LIMIT_MAX_WAIT": "0.02",
        "EPUB_LLM_GLOBAL_HEALTH_ENABLED": "0",
    }


def _spawn_contender(url, prefix, index, ready, release, result):
    """Each process constructs its own real redis-py client and event loops."""
    try:
        _, endpoint = _validated_url(url, "1")
        with _LoopbackGuard(endpoint) as guard, patch.dict(os.environ, _environment(url, prefix)):
            limiter = DistributedLLMTokenBucket()
            try:
                ready.put(index)
                if not release.wait(10):
                    raise AssertionError("parent did not release concurrency barrier")
                successes, denials, keys = 0, 0, []
                for number in range(4):
                    options = dict(provider=HOST, model=ALIASES[(index + number) % len(ALIASES)], estimated_tokens=40)
                    try:
                        lease = (asyncio.run(limiter.acquire(**options)) if number % 2
                                 else limiter.acquire_sync(**options))
                        successes += 1
                        keys.append(lease.token_key)
                    except GatewayWaitTimeout:
                        denials += 1
                result.put(("ok", successes, denials, keys, len(guard.blocked)))
            finally:
                limiter.close()
    except BaseException as exc:
        result.put(("error", type(exc).__name__))


class _CuttableProxy:
    """Forward real Redis TCP traffic, then cut it without stopping Redis."""

    def __init__(self, upstream, guard):
        self.upstream, self.guard = upstream, guard
        self.port = 0
        self.listener = None
        self.connections = set()
        self.lock = threading.Lock()
        self.threads = []
        self.stopped = threading.Event()

    def start(self):
        self.stopped = threading.Event()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(16)
        listener.settimeout(.1)
        self.listener = listener
        self.port = listener.getsockname()[1]
        self.guard.allowed.add(("127.0.0.1", self.port))
        thread = threading.Thread(target=self._accept, args=(listener, self.stopped), daemon=True)
        self.threads.append(thread)
        thread.start()
        return self

    def _accept(self, listener, stopped):
        while not stopped.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self.lock:
                self.connections.add(client)
            thread = threading.Thread(target=self._forward, args=(client, stopped), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _forward(self, client, stopped):
        upstream = None
        try:
            upstream = socket.create_connection(self.upstream, timeout=2)
            with self.lock:
                self.connections.add(upstream)
            while not stopped.is_set():
                readable, _, _ = select.select([client, upstream], [], [], .1)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is client else client).sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            for connection in (client, upstream):
                if connection is not None:
                    with self.lock:
                        self.connections.discard(connection)
                    connection.close()

    def cut(self):
        self.stopped.set()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        with self.lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for thread in self.threads:
            thread.join(2)
        self.threads.clear()


class RedisOptInSafetyTests(unittest.TestCase):
    def test_only_explicit_isolated_loopback_url_is_accepted(self):
        self.assertEqual(_validated_url("redis://127.0.0.1:16379/0", "1")[1], ("127.0.0.1", 16379))
        self.assertEqual(_validated_url("redis://[::1]:16379/1", "1")[1], ("::1", 16379))
        for url, acknowledgement in (
            ("", "1"), ("redis://127.0.0.1:16379/0", "0"),
            ("redis://127.0.0.1:6379/0", "1"), ("redis://localhost:16379/0", "1"),
            ("redis://redis.example.invalid:16379/0", "1"), ("redis://192.0.2.1:16379/0", "1"),
            ("redis://user:secret@127.0.0.1:16379/0", "1"),
            ("redis://127.0.0.1:16379/0?socket_timeout=1", "1"),
            ("redis://127.0.0.1:16379/0#fragment", "1"),
            ("redis://127.0.0.1:16379/16", "1"), ("unix:///tmp/redis.sock", "1"),
        ):
            with self.subTest(url=url, acknowledgement=acknowledgement), self.assertRaises(ValueError):
                _validated_url(url, acknowledgement)


@unittest.skipUnless(os.environ.get("D54_REDIS_URL"), "NOT ACCEPTED: supply an isolated D54_REDIS_URL explicitly")
class RealRedisAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url, cls.endpoint = _validated_url(os.environ["D54_REDIS_URL"], os.environ.get("D54_REDIS_ISOLATED"))
        # A bad explicit endpoint is an error, not a skip or in-memory substitute.
        import redis
        with _LoopbackGuard(cls.endpoint) as guard:
            client = redis.Redis.from_url(cls.url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
            try:
                if client.ping() is not True:
                    raise AssertionError("isolated Redis did not answer PING")
                print("D54 real Redis version:", client.info("server")["redis_version"], flush=True)
                if guard.blocked:
                    raise AssertionError("unexpected network during Redis handshake")
            finally:
                client.close()

    def setUp(self):
        import redis
        self.prefix = "epub:d54:" + uuid.uuid4().hex
        self.environment = patch.dict(os.environ, _environment(self.url, self.prefix))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.guard = _LoopbackGuard(self.endpoint)
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        self.client = redis.Redis.from_url(self.url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
        self.addCleanup(self.client.close)
        self.health_keys = []
        self.addCleanup(self._clean_keys)
        self.limiters = []
        self.tmp = tempfile.TemporaryDirectory(prefix="fixepub-d54-")
        self.addCleanup(self.tmp.cleanup)
        from sqlalchemy import create_engine
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "ledger.db"), connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        self.ledger = get_ledger(self.engine)

    def tearDown(self):
        for limiter in self.limiters:
            limiter.close()
        self.assertEqual(self.guard.blocked, [], "external network attempt is not an accepted test")

    def _clean_keys(self):
        keys = list(self.client.scan_iter(match=self.prefix + ":*", count=100))
        for key in keys:
            if not key.startswith(self.prefix + ":"):
                raise AssertionError("refusing to delete an unowned key")
        if keys or self.health_keys:
            self.client.delete(*(keys + self.health_keys))
        self.assertEqual(list(self.client.scan_iter(match=self.prefix + ":*")), [])

    def limiter(self, *, rpm=None, tpm=None, url=None):
        limiter = DistributedLLMTokenBucket()
        if rpm is not None:
            limiter.rpm = rpm
        if tpm is not None:
            limiter.tpm = tpm
        if url is not None:
            limiter.redis_url = url
        self.limiters.append(limiter)
        return limiter

    @staticmethod
    def args(model=MODEL, cost=40):
        return dict(provider=HOST, model=model, estimated_tokens=cost)

    def metadata(self, limiter, **extra):
        return dict(model=MODEL, base_url=HOST, messages=MESSAGES, limiter=limiter, **extra)

    @staticmethod
    def response():
        return {"model": MODEL, "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10}}

    def balance(self, key):
        return float(self.client.hget(key, "tokens"))

    def test_production_lua_aliases_and_async_loops_share_a_single_bucket(self):
        limiter = self.limiter()
        leases = [limiter.acquire_sync(**self.args(ALIASES[0]))]
        leases.extend(asyncio.run(self.limiter().acquire(**self.args(alias))) for alias in ALIASES[1:])
        self.assertEqual(len({lease.token_key for lease in leases}), 1)
        self.assertEqual(len({lease.reservation_key for lease in leases}), 3)
        for lease in leases:
            self.assertEqual(self.client.hget(lease.reservation_key, "estimated"), "40")
            self.assertEqual(self.client.hget(lease.reservation_key, "settled"), "0")
            self.assertGreater(self.client.pttl(lease.reservation_key), 7_190_000)
        self.assertGreaterEqual(self.balance(leases[0].token_key), 880)
        self.assertLess(self.balance(leases[0].token_key), 890)
        self.assertEqual(len(list(self.client.scan_iter(match=self.prefix + ":*:requests"))), 1)

    def test_independent_processes_obey_one_atomic_rpm_capacity(self):
        context = multiprocessing.get_context("spawn")
        ready, result, release = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=_spawn_contender, args=(self.url, self.prefix, i, ready, release, result)) for i in range(4)]
        try:
            for process in processes:
                process.start()
            self.assertEqual({ready.get(timeout=15) for _ in processes}, set(range(4)))
            started = time.monotonic()
            release.set()
            outcomes = [result.get(timeout=10) for _ in processes]
            self.assertLess(time.monotonic() - started, 10, "must finish before RPM=5 can refill a full request")
            self.assertTrue(all(row[0] == "ok" for row in outcomes), outcomes)
            self.assertEqual(sum(row[1] for row in outcomes), 5)
            self.assertEqual(sum(row[2] for row in outcomes), 11)
            self.assertEqual(sum(row[4] for row in outcomes), 0)
            self.assertEqual(len({key for row in outcomes for key in row[3]}), 1)
            self.assertEqual(len(list(self.client.scan_iter(match=self.prefix + ":*:lease:*"))), 5)
        finally:
            release.set()
            for process in processes:
                process.join(3)
                if process.is_alive():
                    process.terminate()
                    process.join(3)
            ready.close()
            result.close()
        self.assertEqual([process.exitcode for process in processes], [0] * 4)

    def test_parallel_tpm_reservations_never_clip_or_overspend(self):
        barrier = threading.Barrier(12)
        def work(index):
            limiter = self.limiter(rpm=1000, tpm=100)
            barrier.wait(timeout=5)
            try:
                return limiter.acquire_sync(**self.args(ALIASES[index % 3]))
            except GatewayWaitTimeout:
                return None
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=12) as pool:
            leases = [lease for lease in pool.map(work, range(12)) if lease is not None]
        self.assertLess(time.monotonic() - started, 5, "no additional 40-token reservation can refill in this window")
        self.assertEqual(len(leases), 2)
        self.assertGreaterEqual(self.balance(leases[0].token_key), 20)
        self.assertLess(self.balance(leases[0].token_key), 30)

    def test_real_eval_replay_is_idempotent_and_settlement_is_atomic(self):
        limiter = self.limiter(tpm=100)
        with patch.object(limiter, "_eval", wraps=limiter._eval) as observed:
            lease = limiter.acquire_sync(**self.args())
        args = observed.call_args.args
        self.assertEqual(args[0], _ACQUIRE_LUA)
        before = self.balance(lease.token_key)
        for _ in range(4):
            self.assertEqual(self.client.eval(*args), [1, 0])
        self.assertEqual(self.balance(lease.token_key), before)
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _: limiter.reconcile_sync(lease, actual_tokens=10), range(36)))
        self.assertEqual(self.balance(lease.token_key), before + 30)
        self.assertEqual(self.client.hget(lease.reservation_key, "settled"), "1")
        self.assertEqual(self.client.eval(_ADJUST_LUA, 2, lease.token_key, lease.reservation_key, 100, 0), 0)
        self.assertEqual(self.balance(lease.token_key), before + 30)

    def test_actual_overuse_remains_debt_and_unknown_usage_never_refunds(self):
        limiter = self.limiter(tpm=100)
        lease = limiter.acquire_sync(**self.args())
        before = self.balance(lease.token_key)
        limiter.reconcile_sync(lease, actual_tokens=None)
        self.assertEqual(self.balance(lease.token_key), before)
        self.assertEqual(self.client.hget(lease.reservation_key, "settled"), "0")
        limiter.reconcile_sync(lease, actual_tokens=200)
        self.assertEqual(self.balance(lease.token_key), -100)
        with self.assertRaises(GatewayWaitTimeout):
            limiter.acquire_sync(**self.args(cost=1))

    def test_old_generation_cannot_refund_recreated_bucket(self):
        limiter = self.limiter()
        old = limiter.acquire_sync(**self.args())
        old_generation = self.client.hget(old.token_key, "generation")
        self.client.delete(old.token_key)  # Simulates only this test bucket expiring.
        new = limiter.acquire_sync(**self.args())
        self.assertNotEqual(old_generation, self.client.hget(new.token_key, "generation"))
        before = self.balance(new.token_key)
        limiter.reconcile_sync(old, actual_tokens=0)
        self.assertEqual(self.balance(new.token_key), before)
        self.assertEqual(self.client.hget(old.reservation_key, "settled"), "1")

    def test_redis_server_time_refills_without_trusting_local_wall_clock(self):
        limiter = self.limiter(rpm=1, tpm=100)
        lease = limiter.acquire_sync(**self.args())
        with self.assertRaises(GatewayWaitTimeout):
            limiter.acquire_sync(**self.args())
        seconds, microseconds = self.client.time()
        one_minute_ago = seconds * 1000 + microseconds // 1000 - 60001
        request_key = lease.token_key.removesuffix(":tokens") + ":requests"
        for key in (request_key, lease.token_key):
            self.client.hset(key, "updated", one_minute_ago)
        with patch("app.infra.llm_token_bucket.time.time", return_value=-10**12):
            fresh = limiter.acquire_sync(**self.args())
        self.assertTrue(fresh.enabled)
        self.assertEqual(self.balance(fresh.token_key), 60)
        with self.assertRaises(GatewayWaitTimeout):
            limiter.acquire_sync(**self.args())

    def test_oversize_gateway_request_has_no_lua_no_provider_no_ledger(self):
        limiter = self.limiter(tpm=10)
        physical = Mock(return_value=self.response())
        with usage_scope("oversize", "attempt", engine=self.engine), self.assertRaises(GatewayRequestTooLarge):
            governed_call(physical, **self.metadata(limiter))
        physical.assert_not_called()
        self.assertIsNone(limiter._redis)
        self.assertEqual(self.ledger.requests("oversize"), [])
        self.assertEqual(list(self.client.scan_iter(match=self.prefix + ":*")), [])

    def test_missing_provider_usage_preserves_real_reservation_and_unknown_ledger(self):
        leases = []
        physical = AsyncMock(return_value={"model": MODEL})
        with usage_scope("missing", "attempt", engine=self.engine):
            asyncio.run(governed_request(physical, **self.metadata(self.limiter(), on_lease=leases.append)))
        physical.assert_awaited_once()
        self.assertEqual(len(leases), 1)
        lease = leases[0]
        self.assertEqual(self.balance(lease.token_key), 1000 - lease.estimated_tokens)
        self.assertEqual(self.client.hget(lease.reservation_key, "settled"), "0")
        row = self.ledger.requests("missing")[0]
        self.assertEqual(row["request_status"], "response")
        self.assertEqual(row["usage_status"], "missing")
        self.assertIsNone(row["total_tokens"])
        self.assertIsNone(row["calculated_cost"])

    def test_five_physical_stages_share_real_quota_and_each_own_ledger_row(self):
        stages = ("book_profile", "glossary", "body", "failed_chunk_rescue", "precision_polish")
        accepted, release = threading.Event(), threading.Event()
        lock, calls, leases = threading.Lock(), [], []
        def entered(stage):
            with lock:
                calls.append(stage)
                if len(calls) == len(stages):
                    accepted.set()
        def work(stage):
            limiter = self.limiter(tpm=10000)
            options = self.metadata(limiter, stage=stage, on_lease=leases.append)
            with usage_scope(stage, "attempt", engine=self.engine):
                if stage == "precision_polish":
                    def physical():
                        entered(stage)
                        if not release.wait(8):
                            raise AssertionError("test release timed out")
                        return self.response()
                    return governed_call(physical, **options)
                async def physical():
                    entered(stage)
                    if not await asyncio.to_thread(release.wait, 8):
                        raise AssertionError("test release timed out")
                    return self.response()
                return asyncio.run(governed_request(physical, **options))
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(work, stage) for stage in stages]
            try:
                self.assertTrue(accepted.wait(6), "five stages must be simultaneously in flight")
                denied = AsyncMock(return_value=self.response())
                with usage_scope("sixth", "attempt", engine=self.engine), self.assertRaises(GatewayWaitTimeout):
                    asyncio.run(governed_request(denied, **self.metadata(self.limiter(tpm=10000))))
                denied.assert_not_called()
                self.assertEqual(self.ledger.requests("sixth"), [])
                for stage in stages:
                    self.assertEqual(self.ledger.requests(stage)[0]["request_status"], "in_flight")
            finally:
                release.set()
            for future in futures:
                self.assertEqual(future.result(timeout=5), self.response())
        self.assertCountEqual(calls, stages)
        self.assertEqual(len({lease.token_key for lease in leases}), 1)
        self.assertEqual(len(leases), 5)
        self.assertEqual(len(list(self.client.scan_iter(match=self.prefix + ":*:lease:*"))), 5)
        for lease in leases:
            self.assertEqual(self.client.hget(lease.reservation_key, "settled"), "1")
        for stage in stages:
            rows = self.ledger.requests(stage)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["stage"], rows[0]["request_status"], rows[0]["total_tokens"]), (stage, "response", 13))

    def test_real_tcp_cut_fails_closed_then_same_limiter_recovers(self):
        proxy = _CuttableProxy(self.endpoint, self.guard).start()
        self.addCleanup(proxy.cut)
        parts = urlsplit(self.url)
        proxy_url = urlunsplit(("redis", "127.0.0.1:" + str(proxy.port), parts.path, "", ""))
        limiter = self.limiter(url=proxy_url)
        first = limiter.acquire_sync(**self.args())
        self.assertEqual(self.client.hget(first.reservation_key, "estimated"), "40")
        proxy.cut()
        rejected = AsyncMock(return_value=self.response())
        with usage_scope("outage", "attempt", engine=self.engine), self.assertRaises(GatewayUnavailable):
            asyncio.run(governed_request(rejected, **self.metadata(limiter)))
        rejected.assert_not_called()
        self.assertEqual(self.ledger.requests("outage"), [])
        with self.assertRaises(GatewayUnavailable):
            limiter.reconcile_sync(first, actual_tokens=10)
        self.assertEqual(self.client.hget(first.reservation_key, "settled"), "0")
        self.assertTrue(limiter.enabled)
        self.assertTrue(self.client.ping(), "operator Redis must remain alive")
        proxy.start()
        limiter.reconcile_sync(first, actual_tokens=10)
        self.assertEqual(self.client.hget(first.reservation_key, "settled"), "1")
        self.assertTrue(limiter.acquire_sync(**self.args()).enabled)

    def test_cut_after_provider_response_keeps_ledger_and_does_not_repeat_request(self):
        proxy = _CuttableProxy(self.endpoint, self.guard).start()
        self.addCleanup(proxy.cut)
        parts = urlsplit(self.url)
        proxy_url = urlunsplit(("redis", "127.0.0.1:" + str(proxy.port), parts.path, "", ""))
        limiter = self.limiter(url=proxy_url)
        leases = []
        def finish_then_cut():
            proxy.cut()
            return self.response()
        physical = Mock(side_effect=finish_then_cut)
        with usage_scope("lateoutage", "attempt", engine=self.engine), self.assertRaises(GatewayUnavailable):
            governed_call(physical, **self.metadata(limiter, on_lease=leases.append))
        physical.assert_called_once()
        row = self.ledger.requests("lateoutage")[0]
        self.assertEqual((row["request_status"], row["total_tokens"]), ("response", 13))
        self.assertEqual(self.client.hget(leases[0].reservation_key, "settled"), "0")
        self.assertTrue(self.client.ping())

    def test_route_health_real_lua_is_atomic_shared_and_bounded(self):
        # This random host is only a hash input; it is never resolved or dialed.
        host = uuid.uuid4().hex + ".d54.invalid"
        first = ("https://" + host + "/v1", MODEL)
        alias = (host, "deepseek-v4-flash")
        with patch.dict(os.environ, {"EPUB_LLM_GLOBAL_HEALTH_ENABLED": "1"}):
            health = DistributedRouteHealth()
        self.addCleanup(health.close)
        key = health._key(first)
        self.health_keys.append(key)
        self.assertEqual(health._key(alias), key)
        with ThreadPoolExecutor(max_workers=10) as pool:
            self.assertEqual(list(pool.map(lambda _: health.record_failure(first), range(30))), [True] * 30)
        self.assertEqual(health.snapshot([alias])[alias]["failures"], 10)
        self.assertTrue(health.record_success(alias, 100))
        snapshot = health.snapshot([first])[first]
        self.assertEqual(snapshot["failures"], 9)
        self.assertEqual(snapshot["cooldown_until_epoch"], 0)
        self.assertGreater(self.client.ttl(key), 0)


if __name__ == "__main__":
    if not os.environ.get("D54_REDIS_URL"):
        print("D54 NOT RUN / NOT ACCEPTED: explicit isolated D54_REDIS_URL and D54_REDIS_ISOLATED=1 are required", file=sys.stderr)
        raise SystemExit(2)
    unittest.main(verbosity=2)
