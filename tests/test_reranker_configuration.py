"""Tests for Reranker centralized configuration and provider routing (ALIGN-02)."""

from unittest.mock import MagicMock, patch

import pytest

from config import settings
from rag.models import Chunk
from rag.reranker import Reranker


def test_reranker_siliconflow_routing(monkeypatch):
    monkeypatch.setattr(settings, "rerank_provider", "siliconflow")
    monkeypatch.setattr(settings, "rerank_model", "")
    monkeypatch.setattr(settings, "rerank_base_url", "")
    monkeypatch.setattr(settings, "bailian_api_key", "sk-silicon-test")

    reranker = Reranker()
    assert reranker.provider == "siliconflow"
    assert reranker.model == "BAAI/bge-reranker-v2-m3"
    assert reranker.url == "https://api.siliconflow.cn/v1/rerank"

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [{"index": 0, "relevance_score": 0.95}]
    }
    mock_client.post.return_value = mock_resp

    with patch.object(reranker, "_get_client", return_value=mock_client):
        docs = [Chunk(text="测试文本", doc_name="doc.txt", chunk_index=0)]
        results = reranker.rerank("查询", docs)

        assert len(results) == 1
        assert results[0].rerank_score == 0.95
        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        call_json = mock_client.post.call_args[1]["json"]
        assert call_url == "https://api.siliconflow.cn/v1/rerank"
        assert call_json["model"] == "BAAI/bge-reranker-v2-m3"


def test_reranker_bailian_routing(monkeypatch):
    monkeypatch.setattr(settings, "rerank_provider", "bailian")
    monkeypatch.setattr(settings, "rerank_model", "")
    monkeypatch.setattr(settings, "rerank_base_url", "")
    monkeypatch.setattr(settings, "bailian_rerank_model", "gte-rerank-v2")
    monkeypatch.setattr(settings, "bailian_api_key", "sk-bailian-test")

    reranker = Reranker()
    assert reranker.provider == "bailian"
    assert reranker.model == "gte-rerank-v2"
    assert reranker.url == settings.bailian_rerank_url

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    # Bailian native output format
    mock_resp.json.return_value = {
        "output": {
            "results": [{"index": 0, "relevance_score": 0.88}]
        }
    }
    mock_client.post.return_value = mock_resp

    with patch.object(reranker, "_get_client", return_value=mock_client):
        docs = [Chunk(text="阿里百炼测试", doc_name="doc.txt", chunk_index=0)]
        results = reranker.rerank("百炼查询", docs)

        assert len(results) == 1
        assert results[0].rerank_score == 0.88
        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        call_json = mock_client.post.call_args[1]["json"]
        assert call_url == settings.bailian_rerank_url
        assert call_json["model"] == "gte-rerank-v2"


def test_reranker_local_routing(monkeypatch):
    monkeypatch.setattr(settings, "rerank_provider", "local")
    monkeypatch.setattr(settings, "rerank_model", "")
    monkeypatch.setattr(settings, "rerank_base_url", "")

    reranker = Reranker()
    assert reranker.provider == "local"
    assert reranker.model == settings.local_rerank_model
    assert reranker.url == settings.local_rerank_url

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [{"index": 0, "relevance_score": 0.77}]
    }
    mock_client.post.return_value = mock_resp

    with patch.object(reranker, "_get_client", return_value=mock_client):
        docs = [Chunk(text="本地模型文本", doc_name="doc.txt", chunk_index=0)]
        results = reranker.rerank("本地查询", docs)

        assert len(results) == 1
        assert results[0].rerank_score == 0.77
        call_url = mock_client.post.call_args[0][0]
        assert call_url == settings.local_rerank_url


def test_reranker_case_insensitivity(monkeypatch):
    monkeypatch.setattr(settings, "rerank_provider", "SiliconFlow")
    reranker1 = Reranker()
    assert reranker1.provider == "siliconflow"

    monkeypatch.setattr(settings, "rerank_provider", "Bailian")
    reranker2 = Reranker()
    assert reranker2.provider == "bailian"

    monkeypatch.setattr(settings, "rerank_provider", "Local")
    reranker3 = Reranker()
    assert reranker3.provider == "local"


def test_reranker_global_model_url_override(monkeypatch):
    monkeypatch.setattr(settings, "rerank_provider", "siliconflow")
    monkeypatch.setattr(settings, "rerank_model", "custom/bge-reranker-large")
    monkeypatch.setattr(settings, "rerank_base_url", "https://proxy.internal/v1/rerank")

    reranker = Reranker()
    assert reranker.model == "custom/bge-reranker-large"
    assert reranker.url == "https://proxy.internal/v1/rerank"


def test_reranker_instance_params_override():
    reranker = Reranker(
        provider="bailian",
        model="custom-gte-model",
        base_url="https://custom.bailian.url/rerank",
        api_key="sk-override-key",
    )
    assert reranker.provider == "bailian"
    assert reranker.model == "custom-gte-model"
    assert reranker.url == "https://custom.bailian.url/rerank"
    assert reranker.api_key == "sk-override-key"


def test_reranker_unknown_provider_raises(monkeypatch):
    monkeypatch.setattr(settings, "rerank_provider", "unknown_provider_xyz")
    reranker = Reranker()
    with pytest.raises(ValueError, match="不支持的重排序提供商"):
        _ = reranker.url
