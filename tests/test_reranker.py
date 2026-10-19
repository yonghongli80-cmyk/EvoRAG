"""Tests for reranker module."""
from unittest.mock import MagicMock, patch

from rag.models import Chunk
from rag.reranker import Reranker


def test_rerank_returns_top_k():
    mock_post_response = MagicMock()
    mock_post_response.status_code = 200
    mock_post_response.json.return_value = {
        "results": [
            {"index": 1, "relevance_score": 0.92},
            {"index": 0, "relevance_score": 0.85},
            {"index": 2, "relevance_score": 0.45},
        ]
    }

    mock_client = MagicMock()
    mock_client.post.return_value = mock_post_response

    with patch.object(Reranker, "_get_client", return_value=mock_client):
        reranker = Reranker()
        docs = [
            Chunk(text="文档A", doc_name="a.md", chunk_index=0),
            Chunk(text="文档B", doc_name="b.md", chunk_index=1),
            Chunk(text="文档C", doc_name="c.md", chunk_index=2),
        ]
        result = reranker.rerank("测试问题", docs, top_k=2)

        assert len(result) == 2
        assert result[0].text == "文档B"
        assert result[1].text == "文档A"
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args[1]
        body = call_kwargs["json"]
        assert body["query"] == "测试问题"
        assert body["documents"] == ["文档A", "文档B", "文档C"]
        assert body["top_n"] == 2


def test_rerank_with_empty_docs():
    with patch.object(Reranker, "_get_client") as mock_get_client:
        reranker = Reranker()
        result = reranker.rerank("问题", [], top_k=5)
        assert result == []
        mock_get_client.assert_not_called()


def test_rerank_uses_cached_client(monkeypatch):
    """The cached client must carry the configured key — and the test must supply that key itself.

    此前这里断言的是运维本地 `.env` 里的真实 key（`config.py` 从 `config.py` 同目录加载 `.env`，
    而 `.env` 不进 git）。于是这条用例在主树里"绿"、在 `git clone` 出来的沙箱里红 ——
    2026-10-19 `tests/test_patch_sandbox.py` 的两条用例正是因此失败：测试依赖了本地环境产物。
    """
    from config import settings

    monkeypatch.setattr(settings, "rerank_api_key", "test-rerank-key")
    reranker = Reranker()
    assert reranker._client is None
    client = reranker._get_client()
    assert client is reranker._get_client()  # cached
    assert client.headers["Authorization"] == "Bearer test-rerank-key"


def test_rerank_preserves_chunk_metadata():
    mock_post_response = MagicMock()
    mock_post_response.status_code = 200
    mock_post_response.json.return_value = {
        "results": [
            {"index": 1, "relevance_score": 0.92},
            {"index": 0, "relevance_score": 0.85},
        ]
    }

    mock_client = MagicMock()
    mock_client.post.return_value = mock_post_response

    with patch.object(Reranker, "_get_client", return_value=mock_client):
        reranker = Reranker()
        docs = [
            Chunk(text="文档A", doc_name="a.md", chunk_index=0),
            Chunk(text="文档B", doc_name="b.md", chunk_index=1),
        ]
        result = reranker.rerank("测试问题", docs, top_k=2)

        assert len(result) == 2
        assert isinstance(result[0], Chunk)
        assert result[0].text == "文档B"
        assert result[0].doc_name == "b.md"
        assert result[1].text == "文档A"
        assert result[1].doc_name == "a.md"
