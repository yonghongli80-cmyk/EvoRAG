"""Redis 缓存模块测试。"""

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture()
def cache_with_mock_redis():
    """创建带 mock Redis 的缓存实例。"""
    mock_client = MagicMock()

    from rag.redis_cache import RedisCache
    cache = RedisCache()
    cache._redis = mock_client
    cache._connected = True

    return cache, mock_client


def test_redis_cache_get_set(cache_with_mock_redis):
    """Redis 缓存应支持 get/set。"""
    cache, mock_redis = cache_with_mock_redis
    mock_redis.get.return_value = '{"answer": "test answer"}'
    result = cache.get("test question")
    assert result["answer"] == "test answer"
    mock_redis.get.assert_called_once()


def test_redis_cache_set(cache_with_mock_redis):
    """Redis 缓存应支持 set。"""
    cache, mock_redis = cache_with_mock_redis
    cache.set("test question", "test answer")
    mock_redis.set.assert_called_once()


def test_redis_cache_get_miss(cache_with_mock_redis):
    """缓存未命中应返回 None。"""
    cache, mock_redis = cache_with_mock_redis
    mock_redis.get.return_value = None
    result = cache.get("nonexistent")
    assert result is None


def test_redis_cache_ttl(cache_with_mock_redis):
    """设置缓存时应带 TTL。"""
    cache, mock_redis = cache_with_mock_redis
    cache.ttl = 600
    cache.set("question", "answer")
    call_args = mock_redis.set.call_args
    assert call_args[1].get("ex") == 600


def test_redis_cache_delete(cache_with_mock_redis):
    """Redis 缓存应支持 delete。"""
    cache, mock_redis = cache_with_mock_redis
    cache.delete("test question")
    mock_redis.delete.assert_called_once()


def test_redis_cache_clear(cache_with_mock_redis):
    """Redis 缓存应支持 clear（使用 scan + delete，不用 flushdb）。"""
    cache, mock_redis = cache_with_mock_redis
    mock_redis.scan.return_value = (0, [b"rag:query:abc", b"rag:query:def"])
    cache.clear()
    mock_redis.scan.assert_called_once()
    mock_redis.delete.assert_called_once()


def test_redis_cache_connection_error_fallback():
    """Redis 连接失败时应降级到内存缓存。"""
    from rag.redis_cache import RedisCache

    cache = RedisCache()
    # 不设置 Redis 连接，模拟连接失败
    cache._redis = None
    cache._connected = False

    # 应该降级到内存缓存，不抛异常
    cache.set("question", "answer")
    result = cache.get("question")
    assert result["answer"] == "answer"


def test_redis_cache_fallback_ttl():
    """内存降级缓存应支持 TTL。"""
    from rag.redis_cache import RedisCache
    import time

    cache = RedisCache(ttl_seconds=1)
    cache._redis = None
    cache._connected = False

    cache.set("question", "answer")
    assert cache.get("question")["answer"] == "answer"

    # 等待 TTL 过期
    time.sleep(1.1)
    assert cache.get("question") is None
