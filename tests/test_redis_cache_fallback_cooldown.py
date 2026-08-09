from unittest.mock import MagicMock

from rag.redis_cache import RedisCache


def test_redis_miss_falls_back_to_value_written_during_outage():
    cache = RedisCache()
    cache._fallback_set(cache._key("question"), '{"answer": "fallback", "sources": []}')
    redis = MagicMock()
    redis.get.return_value = None
    cache._redis = redis
    cache._connected = True

    assert cache.get("question")["answer"] == "fallback"


def test_failed_connection_enters_cooldown(monkeypatch):
    cache = RedisCache()
    cache._redis_retry_after = 100.0
    monkeypatch.setattr("rag.redis_cache.time.monotonic", lambda: 1.0)

    assert cache._get_redis() is None
