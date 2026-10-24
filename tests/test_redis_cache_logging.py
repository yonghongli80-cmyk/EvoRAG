"""Redis connection logs must not disclose authentication material."""

import json
import sys
from types import SimpleNamespace

import pytest

from config import settings


def test_redis_connection_success_log_omits_url_credentials(monkeypatch, caplog):
    from rag.redis_cache import RedisCache

    redis_url = "redis://service-user:synthetic-password@localhost:6379/0?token=synthetic-query-secret"
    client = SimpleNamespace(ping=lambda: True)
    calls = []

    def from_url(url, **kwargs):
        calls.append((url, kwargs))
        return client

    monkeypatch.setattr(settings, "redis_url", redis_url)
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(from_url=from_url))

    cache = RedisCache()
    assert cache.is_redis_connected is True

    log_text = caplog.text
    assert calls == [(redis_url, {"decode_responses": True})]
    assert "Redis 连接成功" in log_text
    assert redis_url not in log_text
    assert "synthetic-password" not in log_text
    assert "synthetic-query-secret" not in log_text


def test_redis_connection_failure_log_omits_url_and_exception_text(
    monkeypatch, caplog
):
    from rag.redis_cache import RedisCache

    redis_url = "redis://service-user:synthetic-password@localhost:6379/0?token=synthetic-query-secret"

    def from_url(url, **kwargs):
        assert url == redis_url
        raise RuntimeError(f"connection refused for {redis_url}")

    monkeypatch.setattr(settings, "redis_url", redis_url)
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(from_url=from_url))

    cache = RedisCache()
    cache.set("question", "fallback answer")
    result = cache.get("question")

    assert cache.is_redis_connected is False
    assert result == {"answer": "fallback answer", "sources": []}
    assert "Redis 连接失败" in caplog.text
    assert "RuntimeError" in caplog.text
    assert redis_url not in caplog.text
    assert "synthetic-password" not in caplog.text
    assert "synthetic-query-secret" not in caplog.text


@pytest.mark.parametrize("operation", ["get", "set", "delete", "clear"])
def test_redis_command_failure_logs_are_credential_safe_and_use_memory_fallback(
    operation, monkeypatch, caplog
):
    from rag.redis_cache import RedisCache

    secrets = (
        "redis://service-user:synthetic-password@localhost:6379/0"
        "?token=synthetic-query-secret"
    )

    def fail(*args, **kwargs):
        raise RuntimeError(f"{operation} failed for {secrets}")

    client = SimpleNamespace(
        ping=lambda: True,
        get=fail if operation == "get" else lambda key: None,
        set=fail if operation == "set" else lambda *args, **kwargs: True,
        delete=fail if operation == "delete" else lambda *keys: 0,
        scan=fail if operation == "clear" else lambda **kwargs: (0, []),
    )
    monkeypatch.setattr(settings, "redis_url", secrets)
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(from_url=lambda *args, **kwargs: client))

    cache = RedisCache()
    assert cache.is_redis_connected is True
    fallback_key = cache._key("question")
    fallback_value = {"answer": "fallback answer", "sources": [{"doc_name": "safe.pdf"}]}
    if operation != "set":
        cache._fallback_set(fallback_key, json.dumps(fallback_value),)

    if operation == "get":
        assert cache.get("question") == fallback_value
    elif operation == "set":
        cache.set("question", "fallback answer", sources=fallback_value["sources"])
        assert cache.get("question") == fallback_value
    elif operation == "delete":
        cache.delete("question")
        assert cache.get("question") is None
    else:
        cache.clear()
        assert cache.get("question") is None

    assert f"Redis {operation} 失败" in caplog.text
    assert "RuntimeError" in caplog.text
    assert secrets not in caplog.text
    assert "synthetic-password" not in caplog.text
    assert "synthetic-query-secret" not in caplog.text
