"""TDD unit tests for DistributedConcurrencyGate (async Redis-backed with in-memory fallback).

Redis 客户端约定为 ``redis.asyncio.Redis``：gate 必须 ``await`` 每一条命令。同步客户端
会在 await 处抛 TypeError 并降级到内存门，这正是本文件重点防守的回归点。
"""

import asyncio
import contextlib
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from rag.llm_pool.concurrency import ConcurrencyLimitExceeded, DistributedConcurrencyGate

# gate 会 await 的 Redis 命令及其“正常路径”默认返回值
_ASYNC_COMMAND_DEFAULTS: dict[str, Any] = {
    "ping": True,
    "incr": 1,
    "decr": 0,
    "expire": True,
    "set": True,
    "zremrangebyscore": 0,
    "zcard": 0,
    "zadd": 1,
}


class _FakeClock:
    """Deterministic stand-in for the ``time`` module used by the gate.

    冷却期断言不该依赖真实时钟（CI 上 10ms 的抖动就会让“冷却中/已过期”的判定翻转）。
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _async_redis_client(side_effects: dict[str, Any] | None = None, **overrides: Any) -> AsyncMock:
    """Build an ``AsyncMock`` standing in for ``redis.asyncio.Redis``.

    注意：``AsyncMock`` 的 *子属性* 是普通 ``MagicMock``，调用它返回的不是可 await 对象。
    因此每一条 gate 要 await 的命令都必须显式替换成自己的 ``AsyncMock``，否则 gate 会在
    await 处拿到 TypeError 并静默降级到内存门 —— 测试会变成假绿。
    """
    client = AsyncMock()
    for name, value in {**_ASYNC_COMMAND_DEFAULTS, **overrides}.items():
        setattr(client, name, AsyncMock(return_value=value))
    for name, effect in (side_effects or {}).items():
        setattr(client, name, AsyncMock(side_effect=effect))
    return client


@pytest.mark.asyncio
async def test_in_memory_concurrency_gate_acquire_and_release():
    """When Redis is None or unreachable, gate must use local in-memory semaphores without error."""
    gate = DistributedConcurrencyGate(redis_client=None)

    # 允许最大并发为 2
    assert await gate.acquire("p1", max_concurrency=2, timeout=0.1) is True
    assert gate.get_inflight("p1") == 1

    assert await gate.acquire("p1", max_concurrency=2, timeout=0.1) is True
    assert gate.get_inflight("p1") == 2

    # 达到并发上限，无法继续获取
    assert await gate.acquire("p1", max_concurrency=2, timeout=0.05) is False

    # 释放一个槽位
    await gate.release("p1")
    assert gate.get_inflight("p1") == 1

    # 现在又可以获取了
    assert await gate.acquire("p1", max_concurrency=2, timeout=0.1) is True
    assert gate.get_inflight("p1") == 2

    await gate.release("p1")
    await gate.release("p1")
    assert gate.get_inflight("p1") == 0


@pytest.mark.asyncio
async def test_in_memory_concurrency_gate_context_manager():
    """Verify async context manager syntax slot()."""
    gate = DistributedConcurrencyGate(redis_client=None)

    async with gate.slot("p1", max_concurrency=1):
        assert gate.get_inflight("p1") == 1
        with pytest.raises(ConcurrencyLimitExceeded):
            async with gate.slot("p1", max_concurrency=1, timeout=0.05):
                pass

    assert gate.get_inflight("p1") == 0


@pytest.mark.asyncio
async def test_in_memory_rpm_rate_limiter():
    """Verify local sliding window rate limiter."""
    gate = DistributedConcurrencyGate(redis_client=None)

    # 限制 3 次/分
    assert await gate.check_rpm_allowed("p1", rpm_limit=3) is True
    assert await gate.check_rpm_allowed("p1", rpm_limit=3) is True
    assert await gate.check_rpm_allowed("p1", rpm_limit=3) is True
    # 第 4 次被限流
    assert await gate.check_rpm_allowed("p1", rpm_limit=3) is False


@pytest.mark.asyncio
async def test_redis_backed_concurrency_gate():
    """Verify Redis mode uses atomic INCR and DECR."""
    mock_redis = _async_redis_client(side_effects={"incr": [1, 2, 3]})

    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    assert await gate.acquire("p1", max_concurrency=2, timeout=0.1) is True
    assert await gate.acquire("p1", max_concurrency=2, timeout=0.1) is True
    # 第 3 次 incr 得到 3 > 2，应 DECR 并返回 False
    assert await gate.acquire("p1", max_concurrency=2, timeout=0.05) is False

    mock_redis.decr.assert_awaited()

    await gate.release("p1")
    assert mock_redis.decr.await_count >= 2


@pytest.mark.asyncio
async def test_redis_commands_are_awaited_not_blocking():
    """(i) Redis 路径的 incr/expire/decr 必须被 await，而不是同步调用阻塞事件循环。

    若 gate 回退成同步调用，``AsyncMock`` 会立刻抛 TypeError 并降级到内存门，
    下面的 await_count 断言就会失败。
    """
    mock_redis = _async_redis_client(side_effects={"incr": [1, 2, 3]}, decr=1)
    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    assert await gate.acquire("p1", max_concurrency=2) is True
    assert await gate.acquire("p1", max_concurrency=2) is True
    # 第三次 incr 返回 3 > 2：必须 await 回滚用的 DECR 并拒绝
    assert await gate.acquire("p1", max_concurrency=2) is False

    assert mock_redis.incr.await_count == 3
    mock_redis.incr.assert_awaited()
    # 只有成功准入的两次续租 TTL
    assert mock_redis.expire.await_count == 2
    mock_redis.expire.assert_awaited()
    # 超限回滚一次
    assert mock_redis.decr.await_count == 1
    mock_redis.decr.assert_awaited()
    # 全程没有走内存门
    assert gate._redis_failed is False
    assert gate._semaphores == {}

    await gate.release("p1")
    assert mock_redis.decr.await_count == 2
    assert gate.get_inflight("p1") == 1


@pytest.mark.asyncio
async def test_redis_rpm_commands_are_awaited():
    """(i) RPM 滑动窗口的每条 Redis 命令同样必须 await。"""
    mock_redis = _async_redis_client(side_effects={"zcard": [0, 1, 2]})
    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    assert await gate.check_rpm_allowed("p1", rpm_limit=2) is True
    assert await gate.check_rpm_allowed("p1", rpm_limit=2) is True
    # 第三次 zcard 返回 2 >= limit，被限流
    assert await gate.check_rpm_allowed("p1", rpm_limit=2) is False

    # 每次都先清理窗口外的旧时间戳，再计数
    assert mock_redis.zremrangebyscore.await_count == 3
    assert mock_redis.zcard.await_count == 3
    # 只有被放行的两次写入窗口 + 续租
    assert mock_redis.zadd.await_count == 2
    assert mock_redis.expire.await_count == 2
    mock_redis.zadd.assert_awaited()
    assert gate._redis_failed is False


@pytest.mark.asyncio
async def test_redis_release_clamps_negative_counter_to_zero():
    """release 走 Redis 时 DECR 结果为负，必须 await SET 归零，且本地在途数不为负。"""
    mock_redis = _async_redis_client(decr=-3)
    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    await gate.release("p1")

    mock_redis.decr.assert_awaited_once_with("rag:llm:inflight:p1")
    mock_redis.set.assert_awaited_once_with("rag:llm:inflight:p1", 0)
    assert gate.get_inflight("p1") == 0


@pytest.mark.asyncio
async def test_redis_failure_falls_back_gracefully():
    """If Redis raises unexpected error during acquire, gate must fall back to memory without crashing."""
    mock_redis = _async_redis_client(side_effects={"incr": Exception("Redis connection dropped")})

    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    # 不会抛出异常，而是优雅退化为本地内存信号量
    acquired = await gate.acquire("p1", max_concurrency=1, timeout=0.1)
    assert acquired is True
    assert gate.get_inflight("p1") == 1
    await gate.release("p1")
    assert gate.get_inflight("p1") == 0


@pytest.mark.asyncio
async def test_redis_failure_degrades_to_memory_without_leaking_permits():
    """(ii) Redis 抛错必须降级到内存门：不放行无限并发，也不会泄漏 permit。"""
    mock_redis = _async_redis_client(side_effects={"incr": ConnectionError("redis down")})
    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    # 第一次：Redis 抛错 → 内存门必须仍然给出 1 个 permit
    assert await gate.acquire("p1", max_concurrency=1) is True
    assert gate.get_inflight("p1") == 1
    assert gate._redis_failed is True
    assert gate._redis_retry_after > 0.0  # 已进入冷却期

    # 降级后依然受 max_concurrency 约束，不是无限放行
    assert await gate.acquire("p1", max_concurrency=1, timeout=0.05) is False
    # 冷却期内不再反复去打故障 Redis（只尝试过一次）
    assert mock_redis.incr.await_count == 1

    # 释放后 permit 归还，没有泄漏
    await gate.release("p1")
    assert gate.get_inflight("p1") == 0
    assert await gate.acquire("p1", max_concurrency=1, timeout=0.05) is True
    assert gate.get_inflight("p1") == 1
    await gate.release("p1")
    assert gate.get_inflight("p1") == 0

    # 连续多轮 acquire/release 后容量恢复到满额（无累积泄漏）
    for _ in range(3):
        assert await gate.acquire("p1", max_concurrency=1, timeout=0.05) is True
        await gate.release("p1")
    assert gate.get_inflight("p1") == 0


@pytest.mark.asyncio
async def test_release_is_safe_when_acquire_used_redis_and_redis_then_fails():
    """错配释放：acquire 走 Redis、release 时 Redis 已故障 → 走内存路径且不抛异常。"""
    mock_redis = _async_redis_client()
    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    assert await gate.acquire("p1", max_concurrency=2) is True
    assert gate.get_inflight("p1") == 1

    # Redis 在 release 之前故障
    mock_redis.decr.side_effect = ConnectionError("redis down during decr")
    await gate.release("p1")  # 不得抛出
    assert gate._redis_failed is True
    assert gate.get_inflight("p1") == 0

    # 随后的重复/多余释放同样安全（内存门不存在时跳过，计数不为负）
    await gate.release("p1")
    await gate.release("p1")
    assert gate.get_inflight("p1") == 0


@pytest.mark.asyncio
async def test_release_tolerates_extra_release_without_raising_or_negative_inflight():
    """多余/错配的释放必须被安全吸收：不抛异常、在途计数不为负、容量不被吃掉。

    注：``asyncio.Semaphore.release()`` 的上界 ``ValueError`` 是 CPython 3.13 才有的行为，
    3.12 上 ``suppress(ValueError)`` 只是版本相关的兜底，因此这里只断言 gate 层的保证
    （不抛异常 / 计数钳到 0 / 正常一轮 acquire+release 后容量不缩水）。
    """
    gate = DistributedConcurrencyGate(redis_client=None)

    assert await gate.acquire("p1", max_concurrency=1) is True
    await gate.release("p1")
    await gate.release("p1")  # 多余释放：不得抛出
    assert gate.get_inflight("p1") == 0

    # 容量没有被“吃掉”：仍然可以正常拿满一个 permit 并归还
    assert await gate.acquire("p1", max_concurrency=1, timeout=0.05) is True
    assert gate.get_inflight("p1") == 1
    await gate.release("p1")
    assert gate.get_inflight("p1") == 0


@pytest.mark.asyncio
async def test_redis_rpm_rate_limiter():
    """Verify Redis mode uses sorted sets for distributed sliding window RPM limit."""
    mock_redis = _async_redis_client(side_effects={"zcard": [0, 2]})

    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    assert await gate.check_rpm_allowed("p1", rpm_limit=2) is True
    mock_redis.zremrangebyscore.assert_awaited()
    mock_redis.zadd.assert_awaited()

    # 第二次 zcard 返回 2 >= limit，应该被限流拒绝
    assert await gate.check_rpm_allowed("p1", rpm_limit=2) is False


@pytest.mark.asyncio
async def test_redis_failure_recovery_after_cooldown(monkeypatch):
    """Verify that after Redis failure cooldown expires and ping succeeds, Redis is recovered."""
    clock = _FakeClock()
    monkeypatch.setattr("rag.llm_pool.concurrency.time", clock)

    mock_redis = _async_redis_client(side_effects={"incr": ConnectionError("temporarily down")})
    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    # 第一次失败，自动使用内存降级
    assert await gate.acquire("p1", max_concurrency=1, timeout=0.1) is True
    assert gate._redis_failed is True
    await gate.release("p1")
    assert mock_redis.decr.await_count == 0  # 冷却期内的释放走内存，不打 Redis

    # 冷却期还没过去：仍然不碰 Redis
    clock.advance(29.0)
    assert await gate.acquire("p1", max_concurrency=1, timeout=0.1) is True
    assert mock_redis.ping.await_count == 0
    await gate.release("p1")

    # 冷却期过去后 ping 成功，Redis 恢复工作
    clock.advance(2.0)
    mock_redis.incr = AsyncMock(side_effect=[1])
    assert await gate.acquire("p1", max_concurrency=2, timeout=0.1) is True
    assert gate._redis_failed is False
    mock_redis.ping.assert_awaited_once()  # 冷却结束后先 ping 探活，再走 Redis
    assert mock_redis.incr.await_count == 1
    await gate.release("p1")


@pytest.mark.asyncio
async def test_redis_failure_cooldown_blocks_retry_for_30_seconds(monkeypatch):
    """冷却期内不得再打 Redis；默认冷却时长必须是 30 秒。"""
    clock = _FakeClock()
    monkeypatch.setattr("rag.llm_pool.concurrency.time", clock)

    mock_redis = _async_redis_client(side_effects={"incr": ConnectionError("redis down")})
    gate = DistributedConcurrencyGate(redis_client=mock_redis)
    assert gate._redis_cooldown_seconds == 30.0

    assert await gate.acquire("p1", max_concurrency=5) is True
    assert gate._redis_failed is True
    # retry_after 精确等于“失败时刻 + 30s”
    assert gate._redis_retry_after == pytest.approx(clock.now + 30.0)

    # 冷却期内（+29s）多次调用都留在内存门，不再触碰 Redis
    clock.advance(29.0)
    for _ in range(3):
        assert await gate.acquire("p1", max_concurrency=5, timeout=0.05) is True
    assert mock_redis.incr.await_count == 1
    assert mock_redis.ping.await_count == 0
    assert gate._redis_retry_after == pytest.approx(clock.now + 1.0)

    for _ in range(4):
        await gate.release("p1")
    assert gate.get_inflight("p1") == 0
    assert mock_redis.decr.await_count == 0  # 释放同样没有穿透冷却期

    # 冷却期一过，就会出现一次 ping 探活（此处仍失败 → 重新进入下一个 30s 冷却）
    clock.advance(2.0)
    assert await gate.acquire("p1", max_concurrency=5) is True
    assert mock_redis.ping.await_count == 1
    assert mock_redis.incr.await_count == 2
    assert gate._redis_retry_after == pytest.approx(clock.now + 30.0)
    await gate.release("p1")


@pytest.mark.asyncio
async def test_slow_redis_does_not_block_event_loop_and_degrades():
    """真实 redis.asyncio 客户端 + 只收不答的假 Redis：慢 Redis 不得阻塞事件循环。

    这是本次缺陷的核心验收点：若 gate 退回同步调用，事件循环会被卡住，ticks 会停在 0。
    """
    import redis.asyncio as aioredis

    held: list[Any] = []
    release = asyncio.Event()

    async def silent_server(reader, writer):
        # 接受连接但永不回复，模拟“黑洞”式慢 Redis
        held.append((reader, writer))
        await release.wait()

    try:
        server = await asyncio.start_server(silent_server, "127.0.0.1", 0)
    except OSError as exc:  # pragma: no cover - 极端受限环境无法绑定回环端口
        pytest.skip(f"cannot bind loopback port: {exc}")

    port = server.sockets[0].getsockname()[1]
    client = aioredis.from_url(
        f"redis://127.0.0.1:{port}/0",
        socket_connect_timeout=0.3,
        socket_timeout=0.3,
    )
    gate = DistributedConcurrencyGate(redis_client=client)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    try:
        started = time.monotonic()
        acquired = await gate.acquire("p1", max_concurrency=1, timeout=1.0)
        elapsed = time.monotonic() - started
    finally:
        ticker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker_task
        release.set()
        await asyncio.sleep(0)  # 让假服务器侧的处理协程退出
        for _, writer in held:
            writer.close()
        await client.aclose()
        server.close()
        # 3.12 的 wait_closed() 会等到所有客户端 transport 关闭，所以必须先关 writer；
        # 这里再加一层超时兜底，避免测试清理阶段挂死整套用例。
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)

    # Redis 卡死 → 不是把请求挂死，而是降级到内存门
    assert acquired is True
    assert gate.get_inflight("p1") == 1
    assert gate._redis_failed is True
    await gate.release("p1")
    assert gate.get_inflight("p1") == 0

    # 等待 Redis 响应期间事件循环仍在调度其它任务（同步阻塞实现会让 ticks 停在 0）
    assert ticks >= 5
    # 卡住时长被 socket_timeout 兜住，而不是 TCP 默认的分钟级超时
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_init_llm_pool_router_wires_async_client(monkeypatch):
    """router 必须注入 redis.asyncio 客户端，且初始化阶段不做阻塞式 ping 探测。"""
    import redis.asyncio as aioredis

    from rag.llm_pool.router import init_llm_pool_router

    ping_probe = AsyncMock(return_value=True)
    monkeypatch.setattr(aioredis.Redis, "ping", ping_probe)

    fake_settings = SimpleNamespace(
        redis_url="redis://127.0.0.1:6399/0",  # 无人监听的端口：初始化不允许去连它
        llm_providers_json="",
        llm_default_model="deepseek",
    )

    started = time.monotonic()
    router = init_llm_pool_router(fake_settings)
    elapsed = time.monotonic() - started

    try:
        assert isinstance(router.gate._redis, aioredis.Redis)  # 不是同步 redis.Redis
        assert ping_probe.await_count == 0  # 初始化不探测，连通性交给 gate 的降级逻辑
        assert elapsed < 1.0
    finally:
        await router.gate._redis.aclose()

    # 未配置 redis_url 时退回纯内存门
    no_redis_router = init_llm_pool_router(SimpleNamespace(redis_url="", llm_providers_json=""))
    assert no_redis_router.gate._redis is None

    # URL 非法时同样降级为纯内存门，不抛异常
    bad_url_router = init_llm_pool_router(SimpleNamespace(redis_url="not-a-redis-url", llm_providers_json=""))
    assert bad_url_router.gate._redis is None
