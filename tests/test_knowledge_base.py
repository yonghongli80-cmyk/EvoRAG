"""Tests for knowledge_base module."""
from unittest.mock import MagicMock, patch

import pytest


@patch("rag.vector_store._get_client")
def test_create_kb(mock_get_client):
    """create_kb should create a collection and return kb_id."""
    from rag.knowledge_base import KnowledgeBaseManager

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    manager = KnowledgeBaseManager()
    kb_id = manager.create_kb("技术部知识库")

    assert kb_id.startswith("kb_")
    mock_client.create_collection.assert_called_once()
    call_args = mock_client.create_collection.call_args
    assert call_args.kwargs["collection_name"] == kb_id


@patch("rag.vector_store._get_client")
def test_list_kbs(mock_get_client):
    """list_kbs should return list of KnowledgeBaseInfo."""
    from rag.knowledge_base import KnowledgeBaseManager

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    mock_collection = MagicMock()
    mock_collection.name = "kb_abc12345"
    mock_client.get_collections.return_value = MagicMock(collections=[mock_collection])
    mock_client.count.return_value = MagicMock(count=10)

    manager = KnowledgeBaseManager()
    kbs = manager.list_kbs()

    assert len(kbs) == 1
    assert kbs[0].kb_id == "kb_abc12345"
    assert kbs[0].doc_count == 10


@patch("rag.vector_store._get_client")
def test_delete_kb(mock_get_client):
    """delete_kb should delete the collection."""
    from rag.knowledge_base import KnowledgeBaseManager

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_get_client.return_value = mock_client

    manager = KnowledgeBaseManager()
    manager.delete_kb("kb_abc12345")

    mock_client.delete_collection.assert_called_once_with("kb_abc12345")


@patch("rag.vector_store.add_to_collection")
@patch("rag.embedder.embed")
@patch("rag.chunker.chunk")
@patch("rag.loader.load")
@patch("rag.vector_store._get_client")
def test_add_document(mock_get_client, mock_load, mock_chunk, mock_embed, mock_add):
    """add_document should load, chunk, embed, and add to collection."""
    from rag.knowledge_base import KnowledgeBaseManager
    from rag.models import Chunk

    mock_get_client.return_value = MagicMock()
    mock_load.return_value = "document content"
    mock_chunk.return_value = [Chunk(text="chunk1", doc_name="test.txt", chunk_index=0)]
    mock_embed.return_value = [[0.1] * 1024]

    manager = KnowledgeBaseManager()
    count = manager.add_document("kb_abc12345", "/path/to/test.txt")

    assert count == 1
    mock_load.assert_called_once_with("/path/to/test.txt")
    mock_add.assert_called_once()


@patch("rag.vector_store.delete_doc")
@patch("rag.vector_store._get_client")
def test_remove_document(mock_get_client, mock_delete_doc):
    """remove_document should delegate to delete_doc."""
    from rag.knowledge_base import KnowledgeBaseManager

    mock_get_client.return_value = MagicMock()

    manager = KnowledgeBaseManager()
    manager.remove_document("kb_abc12345", "test.txt")

    mock_delete_doc.assert_called_once_with("kb_abc12345", "test.txt")


@patch("rag.vector_store._get_client")
def test_list_kbs_empty(mock_get_client):
    """list_kbs should return empty list when no collections exist."""
    from rag.knowledge_base import KnowledgeBaseManager

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_collections.return_value = MagicMock(collections=[])

    manager = KnowledgeBaseManager()
    kbs = manager.list_kbs()

    assert kbs == []


@patch("rag.vector_store._get_client")
def test_list_kbs_filters_non_kb_collections(mock_get_client):
    """list_kbs should only return collections with kb_ prefix."""
    from rag.knowledge_base import KnowledgeBaseManager

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    kb_collection = MagicMock()
    kb_collection.name = "kb_abc12345"
    system_collection = MagicMock()
    system_collection.name = "rag_docs"
    other_collection = MagicMock()
    other_collection.name = "other_collection"

    mock_client.get_collections.return_value = MagicMock(
        collections=[kb_collection, system_collection, other_collection]
    )
    mock_client.count.return_value = MagicMock(count=5)

    manager = KnowledgeBaseManager()
    kbs = manager.list_kbs()

    assert len(kbs) == 1
    assert kbs[0].kb_id == "kb_abc12345"


@patch("rag.vector_store._get_client")
def test_delete_kb_rejects_non_kb_prefix(mock_get_client):
    """delete_kb should reject collections without kb_ prefix."""
    from rag.knowledge_base import KnowledgeBaseManager

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    manager = KnowledgeBaseManager()
    with pytest.raises(ValueError, match="不能删除系统集合"):
        manager.delete_kb("rag_docs")

    mock_client.delete_collection.assert_not_called()


@patch("rag.vector_store._get_client")
def test_delete_kb_allows_kb_prefix(mock_get_client):
    """delete_kb should allow deleting collections with kb_ prefix."""
    from rag.knowledge_base import KnowledgeBaseManager

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    manager = KnowledgeBaseManager()
    manager.delete_kb("kb_abc12345")

    mock_client.delete_collection.assert_called_once_with("kb_abc12345")


@patch("rag.vector_store.search_collection")
@patch("rag.embedder.embed")
@patch("rag.vector_store._get_client")
def test_search_returns_chunks(mock_get_client, mock_embed, mock_search):
    """search should embed query and call search_collection."""
    from rag.knowledge_base import KnowledgeBaseManager
    from rag.models import Chunk

    mock_get_client.return_value = MagicMock()
    mock_embed.return_value = [[0.1] * 1024]
    mock_search.return_value = [
        Chunk(text="result", doc_name="doc.txt", chunk_index=0),
    ]

    manager = KnowledgeBaseManager()
    results = manager.search("kb_abc12345", "test query", top_k=3)

    assert len(results) == 1
    mock_embed.assert_called_once_with(["test query"])
    mock_search.assert_called_once_with("kb_abc12345", [0.1] * 1024, top_k=3)
