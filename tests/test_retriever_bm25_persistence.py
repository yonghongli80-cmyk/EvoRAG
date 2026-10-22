"""Tests for BM25 persistence integration in Retriever.

A-08：索引构建推迟到第一次检索（``_ensure_bm25``），构造阶段只做内存级缓存命中检查
——否则 ``_get_or_create_pipeline`` 会在知识库写锁里做一次全量 scroll + 分词。
"""
from unittest.mock import MagicMock, patch

from rag.bm25_store import BM25Store
from rag.models import Chunk


def _make_chunks(n: int = 3) -> list[Chunk]:
    return [
        Chunk(text=f"chunk text {i}", doc_name=f"doc{i}.md", chunk_index=i)
        for i in range(n)
    ]


def test_retriever_construction_is_lazy_and_uses_bm25store_on_first_use(tmp_path):
    """构造不碰索引；第一次使用时从 BM25Store 加载，而不是全量 scroll。"""
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

        # 构造阶段：廉价、无 I/O。
        MockStore.assert_not_called()
        mock_load.assert_not_called()
        assert retriever.chunks == []
        assert retriever.bm25 is None

        retriever._ensure_bm25()

        mock_instance.load_chunks.assert_called_once_with("col1")
        mock_load.assert_not_called()
        assert retriever.chunks == stored_chunks
        assert retriever.bm25 is not None


def test_retriever_falls_back_to_load_all_on_first_use_when_store_empty(tmp_path):
    """快照为空时第一次使用触发全量重建，并把结果写回 BM25Store。"""
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

        mock_load.assert_not_called()
        retriever._ensure_bm25()

        mock_load.assert_called_once_with("col1")
        mock_instance.save_chunks.assert_called_once_with("col1", fresh_chunks)
        assert retriever.chunks == fresh_chunks
