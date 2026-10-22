"""Phase 1: Qdrant client selection between local path mode and a server URL."""

from unittest.mock import patch

import rag.vector_store as vector_store
from config import Settings, settings


def _patched_client():
    return patch("rag.vector_store.QdrantClient")


def test_qdrant_settings_default_to_local_mode():
    assert Settings.model_fields["qdrant_url"].default == ""
    assert Settings.model_fields["qdrant_api_key"].default == ""


def test_client_uses_local_path_when_url_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "qdrant_api_key", "")
    monkeypatch.setattr(settings, "qdrant_path", str(tmp_path / "qdrant"))

    with _patched_client() as client_cls:
        client = vector_store._get_client()

    client_cls.assert_called_once_with(path=str(tmp_path / "qdrant"))
    assert client is client_cls.return_value


def test_client_uses_url_and_api_key_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")
    monkeypatch.setattr(settings, "qdrant_api_key", "server-api-key")

    with _patched_client() as client_cls:
        client = vector_store._get_client()

    client_cls.assert_called_once_with(url="http://qdrant:6333", api_key="server-api-key")
    assert client is client_cls.return_value


def test_remote_client_omits_empty_api_key(monkeypatch):
    """An empty key must not become an empty ``api-key`` header."""
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")
    monkeypatch.setattr(settings, "qdrant_api_key", "")

    with _patched_client() as client_cls:
        vector_store._get_client()

    client_cls.assert_called_once_with(url="http://qdrant:6333", api_key=None)


def test_whitespace_only_url_keeps_local_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", "   ")
    monkeypatch.setattr(settings, "qdrant_path", str(tmp_path / "qdrant"))

    with _patched_client() as client_cls:
        vector_store._get_client()

    client_cls.assert_called_once_with(path=str(tmp_path / "qdrant"))


def test_client_is_cached_across_calls(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")

    with _patched_client() as client_cls:
        first = vector_store._get_client()
        second = vector_store._get_client()

    assert client_cls.call_count == 1
    assert first is second


def test_close_client_releases_remote_client(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")

    with _patched_client() as client_cls:
        vector_store._get_client()
        vector_store.close_client()

    client_cls.return_value.close.assert_called_once()
    # 关闭后再次获取应重新构造客户端（不是复用已关闭的那个）。
    with _patched_client() as second_cls:
        vector_store._get_client()
    assert second_cls.call_count == 1
