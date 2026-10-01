"""Optional Redis-backed provider/model health shared by all workers."""

from __future__ import annotations

import hashlib
import os

from .llm_token_bucket import route_identity

_UPDATE_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local previous = redis.call('HMGET', KEYS[1], 'failures', 'latency_ms')
local failures = tonumber(previous[1]) or 0
local latency = tonumber(previous[2]) or tonumber(ARGV[2])
if ARGV[1] == 'failure' then
  failures = math.min(10, failures + 1)
  redis.call('HSET', KEYS[1], 'failures', failures, 'cooldown_until_epoch', now + math.min(60, failures * 5))
else
  redis.call('HSET', KEYS[1], 'failures', math.max(0, failures - 1),
    'latency_ms', latency * 0.7 + tonumber(ARGV[2]) * 0.3, 'cooldown_until_epoch', 0)
end
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


class DistributedRouteHealth:
    def __init__(self) -> None:
        self.enabled = (
            os.environ.get("EPUB_LLM_GLOBAL_HEALTH_ENABLED", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.redis_url = (
            os.environ.get("REDIS_URL")
            or os.environ.get("CELERY_BROKER_URL")
            or ""
        ).strip()
        try:
            self.ttl_seconds = max(60, int(os.environ.get("EPUB_LLM_GLOBAL_HEALTH_TTL_SECONDS", "600")))
        except ValueError:
            self.ttl_seconds = 600
        self._redis = None
        if not self.enabled or not self.redis_url:
            self.enabled = False
            return
        try:
            import redis
            self._redis = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
        except Exception as exc:
            self._raise_control(exc)
            self.enabled = False
            self._redis = None

    @staticmethod
    def _key(route: tuple[str, str]) -> str:
        base = route[0] if "://" in route[0] else "https://" + route[0]
        route = route_identity(base, route[1])
        digest = hashlib.sha1(
            f"{route[0]}|{route[1]}".encode("utf-8")
        ).hexdigest()[:20]
        return f"epub:llm:route-health:{digest}"

    def snapshot(
        self,
        routes: list[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, float]]:
        if not self.enabled or not self._redis or not routes:
            return {}
        try:
            pipe = self._redis.pipeline(transaction=False)
            for route in routes:
                pipe.hgetall(self._key(route))
            values = pipe.execute()
            output: dict[tuple[str, str], dict[str, float]] = {}
            for route, value in zip(routes, values):
                if not value:
                    continue
                output[route] = {
                    "failures": float(value.get("failures") or 0),
                    "latency_ms": float(value.get("latency_ms") or 0),
                    "cooldown_until_epoch": float(
                        value.get("cooldown_until_epoch") or 0
                    ),
                }
            return output
        except Exception as exc:
            self._raise_control(exc)
            return {}

    def record_failure(self, route: tuple[str, str]) -> bool:
        if not self.enabled or not self._redis:
            return False
        key = self._key(route)
        try:
            self._redis.eval(_UPDATE_LUA, 1, key, "failure", 0, self.ttl_seconds)
            return True
        except Exception as exc:
            self._raise_control(exc)
            return False

    def record_success(self, route: tuple[str, str], latency_ms: int) -> bool:
        if not self.enabled or not self._redis:
            return False
        key = self._key(route)
        try:
            self._redis.eval(_UPDATE_LUA, 1, key, "success", max(0, latency_ms), self.ttl_seconds)
            return True
        except Exception as exc:
            self._raise_control(exc)
            return False

    @staticmethod
    def _raise_control(exc):
        from billiard.exceptions import SoftTimeLimitExceeded
        from app.cancellation import JobCancelled
        if isinstance(exc, (SoftTimeLimitExceeded, JobCancelled)):
            raise exc

    def close(self):
        if self._redis is not None:
            self._redis.close()
