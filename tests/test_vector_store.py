from unittest.mock import MagicMock, patch

from qdrant_client.models import Distance, VectorParams

from rag.models import Chunk
from rag.vector_store import add, add_to_collection, delete_doc, search, search_collection


@patch("rag.vector_store.QdrantClient")
def test_add_documents(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = False

    add([Chunk(text="doc1", doc_name="", chunk_index=0)], [[0.1] * 1024])

    mock_client.create_collection.assert_called_once_with(
        collection_name="rag_docs",
        vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
    )
    mock_client.upsert.assert_called_once()


@patch("rag.vector_store.QdrantClient")
def test_search_returns_texts(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True

    mock_point = MagicMock()
    mock_point.payload = {"text": "doc content"}
    mock_client.query_points.return_value = MagicMock(points=[mock_point])

    results = search([0.1] * 1024, top_k=3)
    assert isinstance(results[0], Chunk)
    assert results[0].text == "doc content"


@patch("rag.vector_store.QdrantClient")
def test_search_empty(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True
    mock_client.query_points.return_value = MagicMock(points=[])

    results = search([0.1] * 1024, top_k=3)
    assert results == []


@patch("rag.vector_store.QdrantClient")
def test_add_stores_chunk_metadata(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True
    chunks = [Chunk(text="doc1", doc_name="test.txt", chunk_index=0)]
    add(chunks, [[0.1] * 1024])

    call_args = mock_client.upsert.call_args
    point = call_args.kwargs["points"][0]
    assert point.payload["text"] == "doc1"
    assert point.payload["doc_name"] == "test.txt"
    assert point.payload["chunk_index"] == 0


@patch("rag.vector_store.QdrantClient")
def test_search_returns_chunks(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True

    mock_point = MagicMock()
    mock_point.payload = {"text": "doc content", "doc_name": "test.txt", "chunk_index": 2}
    mock_client.query_points.return_value = MagicMock(points=[mock_point])

    results = search([0.1] * 1024, top_k=3)
    assert len(results) == 1
    assert isinstance(results[0], Chunk)
    assert results[0].text == "doc content"
    assert results[0].doc_name == "test.txt"
    assert results[0].chunk_index == 2


@patch("rag.vector_store.QdrantClient")
def test_add_to_collection(mock_client_class):
    """add_to_collection should add chunks to a specific collection."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True
    mock_client.scroll.return_value = ([], None)

    chunks = [Chunk(text="doc1", doc_name="test.txt", chunk_index=0)]
    add_to_collection("my_kb", chunks, [[0.1] * 1024])

    mock_client.upsert.assert_called_once()
    call_args = mock_client.upsert.call_args
    assert call_args.kwargs["collection_name"] == "my_kb"


@patch("rag.vector_store.QdrantClient")
def test_add_to_collection_raises_if_not_exists(mock_client_class):
    """add_to_collection should raise ValueError if collection doesn't exist."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = False

    chunks = [Chunk(text="doc1", doc_name="test.txt", chunk_index=0)]
    import pytest
    with pytest.raises(ValueError, match="不存在"):
        add_to_collection("new_kb", chunks, [[0.1] * 1024])


@patch("rag.vector_store.QdrantClient")
def test_search_collection(mock_client_class):
    """search_collection should search from a specific collection."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True

    mock_point = MagicMock()
    mock_point.payload = {"text": "doc content", "doc_name": "test.txt", "chunk_index": 0}
    mock_client.query_points.return_value = MagicMock(points=[mock_point])

    results = search_collection("my_kb", [0.1] * 1024, top_k=3)

    assert len(results) == 1
    assert results[0].text == "doc content"
    mock_client.query_points.assert_called_once()
    call_args = mock_client.query_points.call_args
    assert call_args.kwargs["collection_name"] == "my_kb"


@patch("rag.vector_store.QdrantClient")
def test_search_collection_empty(mock_client_class):
    """search_collection should return empty list when no results."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True
    mock_client.query_points.return_value = MagicMock(points=[])

    results = search_collection("my_kb", [0.1] * 1024)
    assert results == []


@patch("rag.vector_store.QdrantClient")
def test_delete_doc(mock_client_class):
    """delete_doc should delete documents by doc_name from a collection."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    delete_doc("my_kb", "test.txt")

    mock_client.delete.assert_called_once()
    call_args = mock_client.delete.call_args
    assert call_args.kwargs["collection_name"] == "my_kb"


# ── tags support ────────────────────────────────────────────────────

@patch("rag.vector_store.QdrantClient")
def test_add_to_collection_includes_tags_in_payload(mock_client_class):
    """add_to_collection should include tags in point payload."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True
    mock_client.scroll.return_value = ([], None)

    chunks = [Chunk(text="doc1", doc_name="test.txt", chunk_index=0)]
    add_to_collection("my_kb", chunks, [[0.1] * 1024], tags=["finance", "q3"])

    mock_client.upsert.assert_called_once()
    point = mock_client.upsert.call_args.kwargs["points"][0]
    assert point.payload["tags"] == ["finance", "q3"]


@patch("rag.vector_store.QdrantClient")
def test_add_to_collection_empty_tags_default(mock_client_class):
    """add_to_collection with no tags should use empty list in payload."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True
    mock_client.scroll.return_value = ([], None)

    chunks = [Chunk(text="doc1", doc_name="test.txt", chunk_index=0)]
    add_to_collection("my_kb", chunks, [[0.1] * 1024])

    point = mock_client.upsert.call_args.kwargs["points"][0]
    assert point.payload["tags"] == []


@patch("rag.vector_store.QdrantClient")
def test_search_collection_filters_by_tags(mock_client_class):
    """search_collection with tags should build a filter with FieldCondition for each tag."""
    from qdrant_client.models import Filter

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True

    mock_point = MagicMock()
    mock_point.payload = {"text": "result", "doc_name": "a.txt", "chunk_index": 0}
    mock_client.query_points.return_value = MagicMock(points=[mock_point])

    search_collection("my_kb", [0.1] * 1024, top_k=5, tags=["finance"])

    call_args = mock_client.query_points.call_args
    used_filter = call_args.kwargs["query_filter"]
    assert isinstance(used_filter, Filter)
    assert len(used_filter.must) == 1
    cond = used_filter.must[0]
    assert cond.key == "tags"
    assert cond.match.value == "finance"


@patch("rag.vector_store.QdrantClient")
def test_search_collection_filters_by_doc_name_and_tags(mock_client_class):
    """search_collection with both doc_name and tags should produce combined filter."""
    from qdrant_client.models import Filter

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True

    mock_point = MagicMock()
    mock_point.payload = {"text": "result", "doc_name": "a.txt", "chunk_index": 0}
    mock_client.query_points.return_value = MagicMock(points=[mock_point])

    search_collection("my_kb", [0.1] * 1024, top_k=5, doc_name="a.txt", tags=["finance"])

    call_args = mock_client.query_points.call_args
    used_filter = call_args.kwargs["query_filter"]
    assert isinstance(used_filter, Filter)
    # Should have two conditions: doc_name + tags
    assert len(used_filter.must) == 2
    keys = {c.key for c in used_filter.must}
    assert keys == {"doc_name", "tags"}


@patch("rag.vector_store.QdrantClient")
def test_search_collection_no_filter_when_no_doc_no_tags(mock_client_class):
    """search_collection with no doc_name and no tags should pass None filter."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.collection_exists.return_value = True

    mock_client.query_points.return_value = MagicMock(points=[])

    search_collection("my_kb", [0.1] * 1024, top_k=5)

    call_args = mock_client.query_points.call_args
    assert call_args.kwargs["query_filter"] is None


# ── doc_permission_id support ──────────────────────────────────────

def test_add_to_collection_with_permission_id(tmp_path):
    """add_to_collection 写入 doc_permission_id 到 payload。"""
    from qdrant_client import QdrantClient

    import rag.vector_store as vs
    vs._client = QdrantClient(path=str(tmp_path / "qdrant"))
    vs._ensure_collection_name("test_col")

    from rag.models import Chunk
    chunks = [Chunk(text="hello", doc_name="a.pdf", chunk_index=0)]
    embeddings = [[0.1] * 1024]
    vs.add_to_collection("test_col", chunks, embeddings, doc_permission_id=42)

    client = vs._get_client()
    points, _ = client.scroll(collection_name="test_col", limit=1, with_payload=True)
    assert len(points) == 1
    assert points[0].payload["doc_permission_id"] == 42


def test_search_collection_returns_doc_permission_id(tmp_path):
    """search_collection 返回的 Chunk 包含 doc_permission_id。"""
    from qdrant_client import QdrantClient

    import rag.vector_store as vs
    vs._client = QdrantClient(path=str(tmp_path / "qdrant"))
    vs._ensure_collection_name("test_col")

    from rag.models import Chunk
    chunks = [Chunk(text="hello", doc_name="a.pdf", chunk_index=0)]
    embeddings = [[0.1] * 1024]
    vs.add_to_collection("test_col", chunks, embeddings, doc_permission_id=42)

    results = vs.search_collection("test_col", [0.1] * 1024, top_k=1)
    assert len(results) == 1
    assert results[0].doc_permission_id == 42


def test_add_to_collection_without_permission_id(tmp_path):
    """不传 doc_permission_id 时，payload 中不包含该字段。"""
    from qdrant_client import QdrantClient

    import rag.vector_store as vs
    vs._client = QdrantClient(path=str(tmp_path / "qdrant"))
    vs._ensure_collection_name("test_col")

    from rag.models import Chunk
    chunks = [Chunk(text="hello", doc_name="a.pdf", chunk_index=0)]
    embeddings = [[0.1] * 1024]
    vs.add_to_collection("test_col", chunks, embeddings)

    results = vs.search_collection("test_col", [0.1] * 1024, top_k=1)
    assert len(results) == 1
    assert results[0].doc_permission_id is None
