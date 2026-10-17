"""Distributed concurrency gate and rate limiter backed by Redis with in-memory fallback."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ConcurrencyLimitExceeded(Exception):
    """Raised when an LLM provider reaches its maximum active concurrency limit."""


class DistributedConcurrencyGate:
    """Controls inflight requests and RPM per provider using Redis or local asyncio.Semaphore."""

    def __init__(self, redis_client: Any = None, default_lease_ttl: int = 60) -> None:
        self._redis = redis_client
        self._lease_ttl = default_lease_ttl
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._inflight: dict[str, int] = {}
        self._rpm_windows: dict[str, collections.deque[float]] = {}
        self._redis_failed = False
        self._redis_retry_after = 0.0
        self._redis_cooldown_seconds = 30.0

    def _is_redis_ready(self) -> bool:
        if self._redis is None:
            return False
        if self._redis_failed:
            if time.monotonic() > self._redis_retry_after:
                try:
                    self._redis.ping()
                    self._redis_failed = False
                    self._redis_retry_after = 0.0
                    return True
                except Exception:
                    self._redis_retry_after = time.monotonic() + self._redis_cooldown_seconds
                    return False
            return False
        return True

    def _get_semaphore(self, provider_id: str, max_concurrency: int) -> asyncio.Semaphore:
        if provider_id not in self._semaphores:
            self._semaphores[provider_id] = asyncio.Semaphore(max_concurrency)
        return self._semaphores[provider_id]

    async def acquire(self, provider_id: str, max_concurrency: int, timeout: float = 5.0) -> bool:
        """Acquire a concurrency slot for provider_id. Returns True if acquired, False if limit reached."""
        if self._is_redis_ready():
            try:
                key = f"rag:llm:inflight:{provider_id}"
                val = self._redis.incr(key)
                if val > max_concurrency:
                    self._redis.decr(key)
                    return False
                self._redis.expire(key, self._lease_ttl)
                self._inflight[provider_id] = val
                return True
            except Exception as exc:
                logger.warning("Redis concurrency gate failed for %s (%s); falling back to in-memory gate.", provider_id, exc)
                self._redis_failed = True
                self._redis_retry_after = time.monotonic() + self._redis_cooldown_seconds

        # In-memory fallback path
        sem = self._get_semaphore(provider_id, max_concurrency)
        try:
            await asyncio.wait_for(sem.acquire(), timeout=timeout)
            self._inflight[provider_id] = self._inflight.get(provider_id, 0) + 1
            return True
        except TimeoutError:
            return False

    async def release(self, provider_id: str) -> None:
        """Release a concurrency slot for provider_id."""
        if self._is_redis_ready():
            try:
                key = f"rag:llm:inflight:{provider_id}"
                val = self._redis.decr(key)
                if val < 0:
                    self._redis.set(key, 0)
                    val = 0
                self._inflight[provider_id] = max(0, val)
                return
            except Exception as exc:
                logger.warning("Redis release failed for %s (%s); falling back to in-memory release.", provider_id, exc)
                self._redis_failed = True
                self._redis_retry_after = time.monotonic() + self._redis_cooldown_seconds

        # In-memory release
        sem = self._semaphores.get(provider_id)
        if sem is not None:
            with contextlib.suppress(ValueError):
                sem.release()
        self._inflight[provider_id] = max(0, self._inflight.get(provider_id, 0) - 1)

    def get_inflight(self, provider_id: str) -> int:
        """Get the current count of active inflight requests for a provider."""
        return self._inflight.get(provider_id, 0)

    async def check_rpm_allowed(self, provider_id: str, rpm_limit: int) -> bool:
        """Sliding-window RPM rate limiter. Returns True if request is allowed, False if throttled."""
        now = time.time()
        if self._is_redis_ready():
            try:
                rpm_key = f"rag:llm:rpm:{provider_id}"
                self._redis.zremrangebyscore(rpm_key, 0, now - 60.0)
                current_count = self._redis.zcard(rpm_key)
                if current_count >= rpm_limit:
                    return False
                self._redis.zadd(rpm_key, {f"{now}:{time.perf_counter()}": now})
                self._redis.expire(rpm_key, 90)
                return True
            except Exception as exc:
                logger.warning("Redis RPM check failed for %s (%s); using in-memory window.", provider_id, exc)
                self._redis_failed = True
                self._redis_retry_after = time.monotonic() + self._redis_cooldown_seconds

        window = self._rpm_windows.setdefault(provider_id, collections.deque())

        # 清除 60 秒以前的请求时间戳
        while window and window[0] <= now - 60.0:
            window.popleft()

        if len(window) >= rpm_limit:
            return False

        window.append(now)
        return True

    @contextlib.asynccontextmanager
    async def slot(self, provider_id: str, max_concurrency: int, timeout: float = 5.0):
        """Async context manager for acquiring and releasing a concurrency slot."""
        acquired = await self.acquire(provider_id, max_concurrency=max_concurrency, timeout=timeout)
        if not acquired:
            raise ConcurrencyLimitExceeded(f"Provider '{provider_id}' concurrency limit ({max_concurrency}) reached")
        try:
            yield
        finally:
            await self.release(provider_id)
