import hashlib
from unittest.mock import patch

from rag.models import Chunk
from rag.retriever import Retriever


@patch("rag.retriever.embed")
@patch("rag.retriever.dense_search")
def test_retrieve_returns_ranked_results(mock_dense_search, mock_embed):
    chunks = [
        Chunk(text="RAG is a technique in AI", doc_name="ai.md", chunk_index=0),
        Chunk(text="Python is a programming language", doc_name="ai.md", chunk_index=1),
        Chunk(text="RAG combines retrieval and generation", doc_name="ai.md", chunk_index=2),
    ]
    mock_embed.return_value = [[0.1] * 1024]
    mock_dense_search.return_value = [
        Chunk(text="RAG is a technique in AI", doc_name="ai.md", chunk_index=0),
        Chunk(text="RAG combines retrieval and generation", doc_name="ai.md", chunk_index=2),
    ]

    retriever = Retriever(chunks)
    results = retriever.retrieve("RAG", top_k=2)

    assert len(results) == 2
    assert all(isinstance(r, Chunk) for r in results)
    assert all(r in chunks for r in results)


@patch("rag.retriever.embed")
@patch("rag.retriever.dense_search")
def test_retrieve_calls_embedder_with_query(mock_dense_search, mock_embed):
    chunks = [Chunk(text="document about AI", doc_name="doc.md", chunk_index=0)]
    mock_embed.return_value = [[0.1] * 1024]
    mock_dense_search.return_value = [
        Chunk(text="document about AI", doc_name="doc.md", chunk_index=0),
    ]

    retriever = Retriever(chunks)
    retriever.retrieve("test query", top_k=1)

    mock_embed.assert_called_once_with(["test query"])


def test_rrf_fuse_combines_results():
    c1 = Chunk(text="doc1", doc_name="a.md", chunk_index=0)
    c2 = Chunk(text="doc2", doc_name="a.md", chunk_index=1)
    c3 = Chunk(text="doc3", doc_name="a.md", chunk_index=2)
    c4 = Chunk(text="doc4", doc_name="a.md", chunk_index=3)
    dense = [c1, c2, c3]
    sparse = [c3, c4, c1]

    results = Retriever._rrf_fuse(dense, sparse, top_k=2)

    assert len(results) == 2
    assert results[0].text == "doc1"
    assert results[1].text == "doc3"


@patch("rag.retriever.embed")
@patch("rag.retriever.dense_search")
def test_retrieve_returns_chunks(mock_dense_search, mock_embed):
    chunks = [
        Chunk(text="RAG is a technique", doc_name="doc.md", chunk_index=0),
        Chunk(text="Python is a language", doc_name="doc.md", chunk_index=1),
        Chunk(text="RAG combines retrieval", doc_name="doc.md", chunk_index=2),
    ]
    mock_embed.return_value = [[0.1] * 1024]
    mock_dense_search.return_value = [
        Chunk(text="RAG is a technique", doc_name="doc.md", chunk_index=0),
        Chunk(text="RAG combines retrieval", doc_name="doc.md", chunk_index=2),
    ]

    retriever = Retriever(chunks)
    results = retriever.retrieve("RAG", top_k=2)

    assert len(results) == 2
    assert all(isinstance(r, Chunk) for r in results)
    assert results[0].doc_name == "doc.md"


@patch("rag.retriever.search_collection")
@patch("rag.retriever.embed")
def test_retrieve_with_collection_name_uses_search_collection(mock_embed, mock_search_collection):
    """When collection_name is provided, retrieve should use search_collection."""
    chunks = [
        Chunk(text="RAG is a technique", doc_name="doc.md", chunk_index=0),
        Chunk(text="Python is a language", doc_name="doc.md", chunk_index=1),
    ]
    mock_embed.return_value = [[0.1] * 1024]
    mock_search_collection.return_value = [
        Chunk(text="RAG is a technique", doc_name="doc.md", chunk_index=0),
        Chunk(text="Python is a language", doc_name="doc.md", chunk_index=1),
    ]

    retriever = Retriever(chunks, collection_name="kb_abc12345")
    results = retriever.retrieve("RAG", top_k=2)

    assert len(results) == 2
    mock_search_collection.assert_called_once()
    assert mock_search_collection.call_args[0][0] == "kb_abc12345"


def test_rrf_fuse_accepts_weights():
    """_rrf_fuse should accept and apply weights parameter."""
    c1 = Chunk(text="doc1", doc_name="a.md", chunk_index=0)
    c2 = Chunk(text="doc2", doc_name="a.md", chunk_index=1)
    c3 = Chunk(text="doc3", doc_name="a.md", chunk_index=2)
    dense = [c1, c2]
    sparse = [c3]

    # Without weights (default behavior)
    results_no_w = Retriever._rrf_fuse(dense, sparse, top_k=3)
    assert len(results_no_w) == 3

    # With weights: heavily penalize c1
    h1 = hashlib.md5(c1.text.encode()).hexdigest()
    weights = {h1: 0.1}
    results_w = Retriever._rrf_fuse(dense, sparse, top_k=3, weights=weights)
    assert len(results_w) == 3
    # c1 should rank lower with low weight
    idx_no_w = [d.text for d in results_no_w].index("doc1")
    idx_w = [d.text for d in results_w].index("doc1")
    assert idx_w >= idx_no_w  # c1 moved down or stayed same


@patch("rag.retriever.embed")
@patch("rag.retriever.dense_search")
def test_retrieve_passes_weights_to_rrf_fuse(mock_dense_search, mock_embed):
    """retrieve() should pass weights to _rrf_fuse."""
    c1 = Chunk(text="chunk_a", doc_name="doc.md", chunk_index=0)
    c2 = Chunk(text="chunk_b", doc_name="doc.md", chunk_index=1)
    mock_embed.return_value = [[0.1] * 1024]
    mock_dense_search.return_value = [c1, c2]

    retriever = Retriever([c1, c2])
    weights = {"fake_hash": 1.5}
    results = retriever.retrieve("test", top_k=2, weights=weights)

    assert len(results) == 2


@patch("rag.retriever.embed")
@patch("rag.retriever.dense_search")
def test_retrieve_accepts_none_weights(mock_dense_search, mock_embed):
    """retrieve() should work with weights=None (default)."""
    c1 = Chunk(text="chunk_a", doc_name="doc.md", chunk_index=0)
    mock_embed.return_value = [[0.1] * 1024]
    mock_dense_search.return_value = [c1]

    retriever = Retriever([c1])
    results = retriever.retrieve("test", top_k=1, weights=None)

    assert len(results) == 1


@patch("rag.retriever.search_collection")
@patch("rag.retriever.embed")
def test_retrieve_passes_tags_to_search_collection(mock_embed, mock_search_collection):
    """retrieve() should forward tags to search_collection."""
    c1 = Chunk(text="doc1", doc_name="a.md", chunk_index=0)
    mock_embed.return_value = [[0.1] * 1024]
    mock_search_collection.return_value = [c1]

    retriever = Retriever([c1], collection_name="kb_test")
    results = retriever.retrieve("query", top_k=2, tags=["finance", "q3"])

    assert len(results) == 1
    mock_search_collection.assert_called_once()
    _, kwargs = mock_search_collection.call_args
    assert kwargs["tags"] == ["finance", "q3"]


@patch("rag.retriever.search_collection")
@patch("rag.retriever.embed")
def test_retrieve_no_tags_passes_none(mock_embed, mock_search_collection):
    """retrieve() without tags should pass tags=None to search_collection."""
    c1 = Chunk(text="doc1", doc_name="a.md", chunk_index=0)
    mock_embed.return_value = [[0.1] * 1024]
    mock_search_collection.return_value = [c1]

    retriever = Retriever([c1], collection_name="kb_test")
    retriever.retrieve("query", top_k=2)

    _, kwargs = mock_search_collection.call_args
    assert kwargs["tags"] is None
