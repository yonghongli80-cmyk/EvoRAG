"""BM25 快照必须带版本元数据，否则降级检索拿不出可引用的证据。

v1 快照只存 `(collection, text, doc_name, chunk_index)`，块因此没有
`document_id`/`document_version_id`：密集检索抖动降级纯 BM25 时，这些块在版本化链路里
不可引用（2026-09-30 实测会让整条请求失败）。v2 记录引用所需字段，并把旧快照判为过期，
首次使用时从向量库重建——不需要人工迁移步骤。
"""

import sqlite3
from unittest.mock import patch

from rag.bm25_store import BM25Store
from rag.models import Chunk


def versioned_chunk(index: int) -> Chunk:
    return Chunk(
        text=f"chunk text {index}",
        doc_name=f"doc{index}.md",
        chunk_index=index,
        doc_permission_id=index + 100,
        document_id=index + 1,
        document_version_id=index + 10,
        version_no=1,
        is_current=True,
        validity_status="valid",
    )


def test_snapshot_round_trips_the_metadata_a_citation_needs(tmp_path):
    store = BM25Store(db_path=str(tmp_path / "bm25.db"))
    store.save_chunks("col1", [versioned_chunk(0), versioned_chunk(1)])

    loaded = store.load_chunks("col1")

    assert [(chunk.doc_name, chunk.chunk_index) for chunk in loaded] == [
        ("doc0.md", 0),
        ("doc1.md", 1),
    ]
    assert [chunk.document_id for chunk in loaded] == [1, 2]
    assert [chunk.document_version_id for chunk in loaded] == [10, 11]
    assert [chunk.version_no for chunk in loaded] == [1, 1]
    assert [chunk.validity_status for chunk in loaded] == ["valid", "valid"]
    assert [chunk.doc_permission_id for chunk in loaded] == [100, 101]
    assert store.has_chunks("col1") is True
    store.close()


def test_a_snapshot_without_version_metadata_is_stale(tmp_path):
    """v1 快照（没有版本列）一律视为过期，让调用方从向量库重建。"""
    db = str(tmp_path / "bm25.db")
    legacy = sqlite3.connect(db)
    legacy.execute(
        "CREATE TABLE bm25_chunks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, collection TEXT NOT NULL, text TEXT NOT NULL, "
        "doc_name TEXT NOT NULL, chunk_index INTEGER NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO bm25_chunks (collection, text, doc_name, chunk_index) "
        "VALUES ('col1', 'legacytoken', 'old.md', 0)"
    )
    legacy.commit()
    legacy.close()

    store = BM25Store(db_path=db)

    assert store.has_chunks("col1") is False, "旧结构快照不得被当成可用快照"
    assert store.load_chunks("col1") == []
    # 迁移必须是补列，不是重建表（旧行保留，只是被当作过期）。
    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(bm25_chunks)")}
    assert "document_version_id" in columns
    assert store._conn.execute("SELECT COUNT(*) FROM bm25_chunks").fetchone()[0] == 1
    store.close()


def test_retriever_rebuilds_a_stale_snapshot_and_still_serves_bm25(tmp_path, monkeypatch):
    """重建后的快照带版本元数据：密集检索失败时仍能给出**可引用**的证据。"""
    import rag.bm25_store as bm25_store
    import rag.retriever as retriever_module

    db = str(tmp_path / "bm25.db")
    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", db)
    monkeypatch.setattr(retriever_module.settings, "bm25_db_path", db)
    retriever_module.invalidate_bm25_cache()
    rebuilt = [versioned_chunk(0), versioned_chunk(1)]

    try:
        with patch("rag.retriever._load_all_chunks", return_value=rebuilt) as load_all, patch(
            "rag.retriever.embed", side_effect=RuntimeError("dense offline")
        ):
            retriever = retriever_module.Retriever([], collection_name="col1")
            results = retriever.retrieve("chunk", top_k=2)

        load_all.assert_called_once_with("col1")
        assert [chunk.doc_name for chunk in results] == ["doc0.md", "doc1.md"]
        assert all(chunk.document_version_id is not None for chunk in results)
    finally:
        retriever_module.invalidate_bm25_cache()
