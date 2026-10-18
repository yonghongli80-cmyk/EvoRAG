"""Distributed concurrency gate and rate limiter backed by Redis with in-memory fallback.

Redis 路径使用 ``redis.asyncio`` 异步客户端：``ping`` / ``incr`` / ``decr`` / ``expire`` /
``zadd`` / ``zcard`` / ``zremrangebyscore`` 等命令全部 ``await`` 执行，等待网络往返期间
事件循环可以继续调度其它请求，因此一个慢 Redis 不会再阻塞整个 SSE 服务。
（传入同步客户端会在 await 处抛 ``TypeError``，被下面的降级逻辑捕获并转为内存门，
日志中会留下 warning —— 异步客户端是唯一受支持的 Redis 形态。）
"""

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
    """Controls inflight requests and RPM per provider using Redis or local asyncio.Semaphore.

    ``redis_client`` 必须是异步客户端（``redis.asyncio.Redis``）。所有命令都在事件循环里
    ``await``，不会出现同步阻塞的网络往返。
    """

    def __init__(self, redis_client: Any = None, default_lease_ttl: int = 60) -> None:
        self._redis = redis_client
        self._lease_ttl = default_lease_ttl
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._inflight: dict[str, int] = {}
        self._rpm_windows: dict[str, collections.deque[float]] = {}
        self._redis_failed = False
        self._redis_retry_after = 0.0
        self._redis_cooldown_seconds = 30.0

    async def _is_redis_ready(self) -> bool:
        if self._redis is None:
            return False
        if self._redis_failed:
            if time.monotonic() > self._redis_retry_after:
                try:
                    await self._redis.ping()
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
        """Acquire a concurrency slot for provider_id. Returns True if acquired, False if limit reached.

        准入判定只依赖服务端原子 ``INCR`` 的返回值：即使多个协程在 ``await`` 点交错执行，
        每个协程拿到的 val 都是全局唯一且单调递增的，``val > max_concurrency`` 的协程
        做一次 ``DECR`` 回滚，因此交错不会导致超发（原子性语义与同步客户端版本一致）。
        """
        if await self._is_redis_ready():
            try:
                key = f"rag:llm:inflight:{provider_id}"
                val = await self._redis.incr(key)
                if val > max_concurrency:
                    await self._redis.decr(key)
                    return False
                await self._redis.expire(key, self._lease_ttl)
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
        """Release a concurrency slot for provider_id.

        Redis 路径失败（含 acquire 走 Redis、release 时 Redis 已故障的错配）时降级到内存
        释放：本地 semaphore 不存在就跳过，存在则抑制越界 ``ValueError``，``_inflight``
        用 ``max(0, ...)``/``val < 0`` 归零，所以重复释放或错配释放都不会抛异常、不会变负。
        """
        if await self._is_redis_ready():
            try:
                key = f"rag:llm:inflight:{provider_id}"
                val = await self._redis.decr(key)
                if val < 0:
                    await self._redis.set(key, 0)
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
        """Sliding-window RPM rate limiter. Returns True if request is allowed, False if throttled.

        Redis 路径保持 ``zremrangebyscore``（清理 60s 前的时间戳）→ ``zcard``（计数）→
        ``zadd``（写入本次时间戳）→ ``expire``（90s 兜底 TTL）的滑动窗口语义。
        """
        now = time.time()
        if await self._is_redis_ready():
            try:
                rpm_key = f"rag:llm:rpm:{provider_id}"
                await self._redis.zremrangebyscore(rpm_key, 0, now - 60.0)
                current_count = await self._redis.zcard(rpm_key)
                if current_count >= rpm_limit:
                    return False
                await self._redis.zadd(rpm_key, {f"{now}:{time.perf_counter()}": now})
                await self._redis.expire(rpm_key, 90)
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
