"""Tests for BM25 persistence integration in Retriever."""
from unittest.mock import MagicMock, patch

from rag.bm25_store import BM25Store
from rag.models import Chunk


def _make_chunks(n: int = 3) -> list[Chunk]:
    return [
        Chunk(text=f"chunk text {i}", doc_name=f"doc{i}.md", chunk_index=i)
        for i in range(n)
    ]


def test_retriever_uses_bm25store_when_data_exists(tmp_path):
    """When BM25Store already has chunks, Retriever should NOT call _load_all_chunks."""
    db = str(tmp_path / "test.db")
    store = BM25Store(db_path=db)
    stored_chunks = _make_chunks()
    store.save_chunks("col1", stored_chunks)
    store.close()

    with patch("rag.retriever.BM25Store") as MockStore, \
         patch("rag.retriever._load_all_chunks") as mock_load:
        mock_instance = MagicMock()
        mock_instance.load_chunks.return_value = stored_chunks
        mock_instance.has_chunks.return_value = True
        MockStore.return_value = mock_instance

        from rag.retriever import Retriever
        retriever = Retriever([], collection_name="col1")

        mock_instance.load_chunks.assert_called_once_with("col1")
        mock_load.assert_not_called()
        assert retriever.chunks == stored_chunks


def test_retriever_falls_back_to_load_all_when_store_empty(tmp_path):
    """When BM25Store has no chunks, Retriever should call _load_all_chunks and store into BM25Store."""
    str(tmp_path / "test.db")
    fresh_chunks = _make_chunks(4)

    with patch("rag.retriever.BM25Store") as MockStore, \
         patch("rag.retriever._load_all_chunks", return_value=fresh_chunks) as mock_load:
        mock_instance = MagicMock()
        mock_instance.load_chunks.return_value = []
        mock_instance.has_chunks.return_value = False
        MockStore.return_value = mock_instance

        from rag.retriever import Retriever
        retriever = Retriever([], collection_name="col1")

        mock_load.assert_called_once_with("col1")
        mock_instance.save_chunks.assert_called_once_with("col1", fresh_chunks)
        assert retriever.chunks == fresh_chunks
