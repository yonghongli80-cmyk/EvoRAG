"""TDD unit tests for DistributedConcurrencyGate (Redis-backed with in-memory fallback)."""

from unittest.mock import MagicMock

import pytest

from rag.llm_pool.concurrency import ConcurrencyLimitExceeded, DistributedConcurrencyGate


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
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    # 模拟 INCR 返回 1, 2, 3
    mock_redis.incr.side_effect = [1, 2, 3]

    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    assert await gate.acquire("p1", max_concurrency=2, timeout=0.1) is True
    assert await gate.acquire("p1", max_concurrency=2, timeout=0.1) is True
    # 第 3 次 incr 得到 3 > 2，应 DECR 并返回 False
    assert await gate.acquire("p1", max_concurrency=2, timeout=0.05) is False

    mock_redis.decr.assert_called()

    await gate.release("p1")
    assert mock_redis.decr.call_count >= 2


@pytest.mark.asyncio
async def test_redis_failure_falls_back_gracefully():
    """If Redis raises unexpected error during acquire, gate must fall back to memory without crashing."""
    mock_redis = MagicMock()
    mock_redis.incr.side_effect = Exception("Redis connection dropped")

    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    # 不会抛出异常，而是优雅退化为本地内存信号量
    acquired = await gate.acquire("p1", max_concurrency=1, timeout=0.1)
    assert acquired is True
    assert gate.get_inflight("p1") == 1
    await gate.release("p1")
    assert gate.get_inflight("p1") == 0


@pytest.mark.asyncio
async def test_redis_rpm_rate_limiter():
    """Verify Redis mode uses sorted sets for distributed sliding window RPM limit."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    # 模拟第一次 zcard 为 0，第二次为 2 (limit 为 2)
    mock_redis.zcard.side_effect = [0, 2]

    gate = DistributedConcurrencyGate(redis_client=mock_redis)

    assert await gate.check_rpm_allowed("p1", rpm_limit=2) is True
    mock_redis.zremrangebyscore.assert_called()
    mock_redis.zadd.assert_called()

    # 第二次 zcard 返回 2 >= limit，应该被限流拒绝
    assert await gate.check_rpm_allowed("p1", rpm_limit=2) is False


@pytest.mark.asyncio
async def test_redis_failure_recovery_after_cooldown():
    """Verify that after Redis failure cooldown expires and ping succeeds, Redis is recovered."""
    mock_redis = MagicMock()
    # 第一次报错触发降级
    mock_redis.incr.side_effect = ConnectionError("temporarily down")
    mock_redis.ping.return_value = True

    gate = DistributedConcurrencyGate(redis_client=mock_redis)
    gate._redis_cooldown_seconds = 0.01  # 缩短测试冷却时间

    # 第一次失败，自动使用内存降级
    assert await gate.acquire("p1", max_concurrency=1, timeout=0.1) is True
    assert gate._redis_failed is True
    await gate.release("p1")

    # 等待冷却时间过去
    import asyncio
    await asyncio.sleep(0.02)

    # 此时 ping 成功，Redis 恢复工作
    mock_redis.incr.side_effect = [1]
    assert await gate.acquire("p1", max_concurrency=2, timeout=0.1) is True
    assert gate._redis_failed is False
    await gate.release("p1")

