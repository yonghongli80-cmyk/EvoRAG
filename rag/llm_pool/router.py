"""Router for multi-LLM pool: load balancing, automatic failover, and parallel arena execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

from rag.llm_pool.concurrency import DistributedConcurrencyGate
from rag.llm_pool.models import load_provider_configs_from_settings
from rag.llm_pool.provider import LLMProvider

logger = logging.getLogger(__name__)

# 单例初始化不再做阻塞式探测（见 init_llm_pool_router），因此不再需要 _ROUTER_LOCK：
# 模块级 asyncio.Lock 是死代码（从未被 await），且它无法在同步函数里守卫惰性初始化，
# 把锁绑定到“第一个调用者的 loop”还会在跨 loop 复用时炸掉。惰性初始化现在只做纯内存
# 构造（redis.asyncio 客户端是惰性连接的），并发调用最多多造一个随后被丢弃的客户端对象。
_ROUTER_SINGLETON: LLMPoolRouter | None = None

# 单条 Redis 命令的最长等待时间。async 客户端已经不会阻塞事件循环，但一个“黑洞”式
# 的慢 Redis 会让每个请求各自卡到 TCP 默认超时（分钟级）；超时即按不可用处理，
# 由 gate 降级到内存门并进入 30s 冷却。
_REDIS_CONNECT_TIMEOUT_SECONDS = 1.0
_REDIS_SOCKET_TIMEOUT_SECONDS = 1.0


class LLMPoolRouter:
    """Orchestrates multiple LLM providers: Mode A (load-balanced failover) & Mode B (parallel arena)."""

    def __init__(
        self,
        providers: list[LLMProvider],
        gate: DistributedConcurrencyGate,
        default_provider_id: str = "deepseek",
    ) -> None:
        self.providers: dict[str, LLMProvider] = {p.config.provider_id: p for p in providers}
        self.gate = gate
        self.default_provider_id = default_provider_id

    def get_provider(self, provider_id: str | None = None) -> LLMProvider | None:
        """Get provider by id, or return default / first available provider."""
        if provider_id and provider_id in self.providers:
            return self.providers[provider_id]
        if self.default_provider_id in self.providers:
            return self.providers[self.default_provider_id]
        return next(iter(self.providers.values()), None) if self.providers else None

    def list_models(self) -> list[dict[str, Any]]:
        """Return metadata list of available models for discovery API."""
        return [
            {
                "id": p.config.provider_id,
                "name": p.config.name,
                "model": p.config.model,
                "weight": p.config.weight,
                "healthy": p.is_available(),
                "active_requests": self.gate.get_inflight(p.config.provider_id),
            }
            for p in self.providers.values()
        ]

    def select_balanced_provider(self, preferred_id: str | None = None) -> LLMProvider:
        """Select healthy provider with lowest (inflight + 1) / weight score."""
        if preferred_id and preferred_id in self.providers:
            preferred = self.providers[preferred_id]
            if preferred.is_available():
                return preferred

        candidates = [p for p in self.providers.values() if p.is_available()]
        if not candidates:
            # 降级：如果全部被熔断，尝试未禁用的提供商兜底
            candidates = [p for p in self.providers.values() if p.config.is_active]

        if not candidates:
            raise RuntimeError("No active LLM providers configured in the pool.")

        # Weighted Least-Inflight 算法
        scored = sorted(
            candidates,
            key=lambda p: (self.gate.get_inflight(p.config.provider_id) + 1) / float(p.config.weight),
        )
        return scored[0]

    async def generate_stream_balanced(
        self,
        messages: list[dict],
        preferred_provider_id: str | None = None,
        temperature: float = 0.3,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Mode A: Stream tokens from the best available provider with automatic zero-latency failover."""
        # 确定候选尝试顺序
        ordered_candidates: list[LLMProvider] = []
        if preferred_provider_id and preferred_provider_id in self.providers:
            ordered_candidates.append(self.providers[preferred_provider_id])

        remaining = [p for p in self.providers.values() if p not in ordered_candidates and p.is_available()]
        remaining_sorted = sorted(
            remaining,
            key=lambda p: (self.gate.get_inflight(p.config.provider_id) + 1) / float(p.config.weight),
        )
        ordered_candidates.extend(remaining_sorted)

        if not ordered_candidates:
            ordered_candidates = list(self.providers.values())

        last_error = None
        for provider in ordered_candidates:
            has_yielded = False
            try:
                async with (
                    self.gate.slot(provider.config.provider_id, max_concurrency=provider.config.max_concurrency, timeout=5.0),
                    aclosing(provider.generate_stream(messages, temperature=temperature)) as stream,
                ):
                    async for token in stream:
                        has_yielded = True
                        yield {"type": "token", "model": provider.config.provider_id, "content": token}
                return  # 成功完成整个流式生成
            except Exception as exc:
                last_error = exc
                logger.warning("Provider %s failed during balanced stream: %s", provider.config.provider_id, exc)
                if has_yielded:
                    # 已经开始向客户端吐出 token，无法透明切换到另一个模型，直接抛出
                    raise
                # 尚未吐出任何 token，进行故障转移（Failover）至下一个候选通道
                continue

        raise RuntimeError(f"All LLM providers in pool failed to respond: {last_error}") from last_error

    async def generate_stream_parallel(
        self,
        messages: list[dict],
        provider_ids: list[str],
        temperature: float = 0.3,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Mode B (Arena): Dispatch generation to multiple providers in parallel and multiplex SSE events."""
        targets = [self.providers[pid] for pid in provider_ids if pid in self.providers]
        if not targets:
            default_p = self.get_provider()
            if default_p:
                targets = [default_p]

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def worker(provider: LLMProvider):
            try:
                async with (
                    self.gate.slot(provider.config.provider_id, max_concurrency=provider.config.max_concurrency, timeout=5.0),
                    aclosing(provider.generate_stream(messages, temperature=temperature)) as stream,
                ):
                    async for token in stream:
                        await queue.put({
                            "type": "token",
                            "model": provider.config.provider_id,
                            "content": token,
                        })
                await queue.put({"type": "model_done", "model": provider.config.provider_id})
            except Exception as exc:
                logger.warning("Parallel provider %s failed: %s", provider.config.provider_id, exc)
                await queue.put({
                    "type": "model_error",
                    "model": provider.config.provider_id,
                    "error": str(exc),
                })

        tasks = [asyncio.create_task(worker(p)) for p in targets]
        pending_models = len(tasks)

        try:
            while pending_models > 0:
                item = await queue.get()
                yield item
                if item.get("type") in ("model_done", "model_error"):
                    pending_models -= 1
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        yield {"type": "done"}

    def generate(self, messages: list[dict], preferred_provider_id: str | None = None, temperature: float = 0.3) -> str:
        """Synchronously generate response via best balanced provider."""
        provider = self.select_balanced_provider(preferred_provider_id)
        return provider.generate(messages, temperature=temperature)


def init_llm_pool_router(settings: Any) -> LLMPoolRouter:
    """Construct an LLMPoolRouter instance using global Settings and optional Redis.

    使用 ``redis.asyncio`` 客户端：客户端构造本身不建立连接（惰性连接、无网络 I/O），
    因此这里不再做阻塞式 ``ping()`` 探测，耗时不会超过构造对象的开销。真正的连通性
    由 gate 在首次命令失败时捕获，降级到内存门并进入 30 秒冷却。
    """
    redis_client = None
    redis_url = getattr(settings, "redis_url", "")
    if redis_url:
        try:
            import redis.asyncio as aioredis
            redis_client = aioredis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=_REDIS_CONNECT_TIMEOUT_SECONDS,
                socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.info("Redis not usable for LLM pool gate (%s); using in-memory semaphore.", exc)

    gate = DistributedConcurrencyGate(redis_client=redis_client)
    configs = load_provider_configs_from_settings(settings)
    providers = [LLMProvider(cfg) for cfg in configs]
    default_id = getattr(settings, "llm_default_model", "deepseek")
    return LLMPoolRouter(providers=providers, gate=gate, default_provider_id=default_id)


def get_llm_pool_router() -> LLMPoolRouter:
    """Return the process-wide LLMPoolRouter singleton.

    惰性初始化只做内存构造（Redis 客户端不再做阻塞 ping），首次调用不会卡事件循环，
    并发竞态最多多构造一个随后被丢弃的 router，因此不挂锁。
    """
    global _ROUTER_SINGLETON
    if _ROUTER_SINGLETON is None:
        from config import settings
        _ROUTER_SINGLETON = init_llm_pool_router(settings)
    return _ROUTER_SINGLETON
