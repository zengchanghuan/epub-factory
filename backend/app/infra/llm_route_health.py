"""Optional Redis-backed provider/model health shared by all workers."""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any


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
        self.ttl_seconds = max(
            60,
            int(os.environ.get("EPUB_LLM_GLOBAL_HEALTH_TTL_SECONDS", "600")),
        )
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
        except Exception:
            self.enabled = False
            self._redis = None

    @staticmethod
    def _key(route: tuple[str, str]) -> str:
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
        except Exception:
            self.enabled = False
            return {}

    def record_failure(self, route: tuple[str, str]) -> bool:
        if not self.enabled or not self._redis:
            return False
        key = self._key(route)
        try:
            failures = min(
                10.0,
                float(self._redis.hincrbyfloat(key, "failures", 1)),
            )
            self._redis.hset(key, mapping={
                "cooldown_until_epoch": time.time() + min(60.0, failures * 5.0),
            })
            self._redis.expire(key, self.ttl_seconds)
            return True
        except Exception:
            self.enabled = False
            return False

    def record_success(self, route: tuple[str, str], latency_ms: int) -> bool:
        if not self.enabled or not self._redis:
            return False
        key = self._key(route)
        try:
            current: dict[str, Any] = self._redis.hgetall(key) or {}
            previous_latency = float(current.get("latency_ms") or latency_ms)
            previous_failures = float(current.get("failures") or 0)
            self._redis.hset(key, mapping={
                "latency_ms": previous_latency * 0.7 + max(0, latency_ms) * 0.3,
                "failures": max(0.0, previous_failures - 1),
                "cooldown_until_epoch": 0,
            })
            self._redis.expire(key, self.ttl_seconds)
            return True
        except Exception:
            self.enabled = False
            return False
