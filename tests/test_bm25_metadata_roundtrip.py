"""A-04：重建块必须保留 ``chunk_kind``/tags，BM25 快照与 sparse 路径同样生效。

2026-10-22 审查：Qdrant payload → ``Chunk`` 的重建（``_load_all_chunks``）丢了 ``chunk_kind``，
wiki 分块在 BM25/降级路径被 ACL 当成普通文档误删；``tags`` 只有 dense 半边生效，sparse 路径
完全不看 tags（过滤条件被静默忽略）。
"""

import json

import pytest
from qdrant_client import QdrantClient

from rag.bm25_store import SNAPSHOT_SCHEMA_VERSION, BM25Store
from rag.models import Chunk
from rag.retriever import Retriever, _load_all_chunks, decode_tags


@pytest.fixture
def qdrant(tmp_path, monkeypatch):
    import rag.vector_store as vs

    client = QdrantClient(path=str(tmp_path / "qdrant"))
    monkeypatch.setattr(vs, "_client", client)
    return vs


def wiki_chunk(*, text="运行手册", tags=("ops", "hr")):
    return Chunk(
        text=text,
        doc_name="Runbook",
        chunk_index=0,
        document_id=42,
        document_version_id=101,
        version_no=1,
        chunk_kind="wiki",
        metadata={"tags": json.dumps(list(tags), ensure_ascii=False)},
    )


def test_payload_rebuild_keeps_chunk_kind_and_tags(qdrant):
    """``_load_all_chunks`` 是 BM25 快照的唯一来源；它必须还原 ACL 需要的字段。"""
    from rag.vector_store import _ensure_collection_name, add_to_collection

    _ensure_collection_name("kb_wiki")
    original = wiki_chunk()
    add_to_collection(
        "kb_wiki",
        [original],
        [[0.1] * 1024],
        tags=["ops", "hr"],
        document_id=42,
        document_version_id=101,
    )

    [rebuilt] = _load_all_chunks("kb_wiki")

    assert rebuilt.chunk_kind == "wiki"
    assert decode_tags(rebuilt) == ("ops", "hr")


def test_bm25_snapshot_round_trips_chunk_kind_and_tags(tmp_path):
    store = BM25Store(db_path=str(tmp_path / "bm25.db"))
    try:
        store.save_chunks("kb_wiki", [wiki_chunk()])
        [loaded] = store.load_chunks("kb_wiki")
    finally:
        store.close()

    assert loaded.chunk_kind == "wiki"
    assert decode_tags(loaded) == ("ops", "hr")


def test_bm25_snapshot_schema_bumped_to_record_chunk_kind(tmp_path):
    """结构版本必须升代：v2 快照没有 chunk_kind/tags，不能继续当"当前"。"""
    assert SNAPSHOT_SCHEMA_VERSION == "3"

    store = BM25Store(db_path=str(tmp_path / "bm25.db"))
    try:
        store.save_chunks("kb", [Chunk(text="t", doc_name="d.md", chunk_index=0)])
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(bm25_chunks)")}
    finally:
        store.close()

    assert {"chunk_kind", "tags"} <= columns


def _retriever(chunks):
    return Retriever(list(chunks))


def _tagged(*, text, tags, index=0):
    return Chunk(
        text=text,
        doc_name="doc.md",
        chunk_index=index,
        metadata={"tags": json.dumps(list(tags), ensure_ascii=False)},
        rerank_score=0.5,
        score_source="reranker",
    )


def test_sparse_path_filters_by_tags(monkeypatch):
    """sparse 路径必须应用 tags：否则 tag 过滤在 dense 抖动时被静默绕过。"""
    import rag.retriever as retriever_module

    finance = _tagged(text="finance token", tags=("finance",), index=0)
    engineering = _tagged(text="finance token", tags=("engineering",), index=1)
    retriever = _retriever([finance, engineering])
    monkeypatch.setattr(retriever_module, "embed", lambda _texts: [[0.1] * 1024])
    monkeypatch.setattr(retriever_module, "dense_search", lambda *_a, **_k: [])

    results = retriever.retrieve("finance", top_k=5, tags=["finance"])

    assert [chunk.chunk_index for chunk in results] == [0]


def test_sparse_path_tags_contract_is_explicit_for_chunks_without_tag_data(monkeypatch):
    """没有 tags 元数据的块（旧快照/无 tag 写入）在 tag 过滤下被排除，而不是"全部放行"。"""
    import rag.retriever as retriever_module

    untagged = Chunk(text="finance token", doc_name="doc.md", chunk_index=0)
    retriever = _retriever([untagged])
    monkeypatch.setattr(retriever_module, "embed", lambda _texts: [[0.1] * 1024])
    monkeypatch.setattr(retriever_module, "dense_search", lambda *_a, **_k: [])

    assert retriever.retrieve("finance", top_k=5, tags=["finance"]) == []
    # 没有 tag 过滤时照常返回：契约只覆盖"显式要求 tags"的检索。
    assert [chunk.chunk_index for chunk in retriever.retrieve("finance", top_k=5)] == [0]


def test_retrieve_without_tags_keeps_untagged_chunks(monkeypatch):
    import rag.retriever as retriever_module

    untagged = Chunk(text="plain token", doc_name="doc.md", chunk_index=0)
    retriever = _retriever([untagged])
    monkeypatch.setattr(retriever_module, "embed", lambda _texts: [[0.1] * 1024])
    monkeypatch.setattr(retriever_module, "dense_search", lambda *_a, **_k: [])

    assert [chunk.chunk_index for chunk in retriever.retrieve("plain", top_k=5)] == [0]
