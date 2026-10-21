"""Tests for reranker module."""
from unittest.mock import MagicMock, patch

from rag.models import Chunk
from rag.reranker import Reranker, merge_ranked_chunks


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


def test_merge_keeps_higher_score_from_secondary():
    primary_chunk = Chunk(text="同一块", doc_name="a.md", chunk_index=0, rerank_score=0.40)
    secondary_chunk = Chunk(text="同一块", doc_name="a.md", chunk_index=0, rerank_score=0.90)

    result = merge_ranked_chunks([primary_chunk], [secondary_chunk])

    assert len(result) == 1
    assert result[0] is secondary_chunk


def test_merge_unions_disjoint_chunks_in_score_order():
    primary_top = Chunk(text="主路高分", doc_name="a.md", chunk_index=0, rerank_score=0.50)
    primary_low = Chunk(text="主路低分", doc_name="a.md", chunk_index=1, rerank_score=0.20)
    secondary_top = Chunk(text="改路高分", doc_name="b.md", chunk_index=0, rerank_score=0.90)
    secondary_low = Chunk(text="改路低分", doc_name="b.md", chunk_index=1, rerank_score=0.10)

    result = merge_ranked_chunks([primary_top, primary_low], [secondary_top, secondary_low])

    assert [chunk.text for chunk in result] == ["改路高分", "主路高分", "主路低分", "改路低分"]


def test_merge_prefers_scored_chunk_over_none():
    scored = Chunk(text="有分数", doc_name="a.md", chunk_index=0, rerank_score=0.70)
    unscored = Chunk(text="有分数", doc_name="a.md", chunk_index=0, rerank_score=None)
    assert merge_ranked_chunks([unscored], [scored]) == [scored]

    primary_scored = Chunk(text="另一块", doc_name="b.md", chunk_index=0, rerank_score=0.30)
    secondary_unscored = Chunk(text="另一块", doc_name="b.md", chunk_index=0, rerank_score=None)
    result = merge_ranked_chunks([primary_scored], [secondary_unscored])
    assert len(result) == 1
    assert result[0] is primary_scored


def test_merge_both_unscored_keeps_first_seen_order():
    primary_a = Chunk(text="主路A", doc_name="a.md", chunk_index=0, rerank_score=None)
    primary_b = Chunk(text="主路B", doc_name="b.md", chunk_index=0, rerank_score=None)
    secondary_b = Chunk(text="主路B", doc_name="b.md", chunk_index=0, rerank_score=None)
    secondary_c = Chunk(text="改路C", doc_name="c.md", chunk_index=0, rerank_score=None)

    result = merge_ranked_chunks([primary_a, primary_b], [secondary_b, secondary_c])

    assert [chunk.text for chunk in result] == ["主路A", "主路B", "改路C"]
    assert result[1] is primary_b


def test_merge_empty_inputs():
    assert merge_ranked_chunks([], []) == []

    only_primary = Chunk(text="只有主路", doc_name="a.md", chunk_index=0, rerank_score=0.50)
    assert merge_ranked_chunks([only_primary], []) == [only_primary]
    assert merge_ranked_chunks([], [only_primary]) == [only_primary]


def test_merge_equal_scores_keeps_primary_object():
    primary_chunk = Chunk(text="同分块", doc_name="a.md", chunk_index=0, rerank_score=0.60)
    secondary_chunk = Chunk(text="同分块", doc_name="a.md", chunk_index=0, rerank_score=0.60)

    result = merge_ranked_chunks([primary_chunk], [secondary_chunk])

    assert len(result) == 1
    assert result[0] is primary_chunk
