"""BM25 snapshots must follow Qdrant collection mutations."""

from qdrant_client import QdrantClient

from rag.models import Chunk


def test_bm25_rebuilds_after_document_add_and_delete(tmp_path, monkeypatch):
    import rag.bm25_store as bm25_store
    import rag.retriever as retriever_module
    import rag.vector_store as vector_store

    vector_store._client = QdrantClient(path=str(tmp_path / "qdrant"))
    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    monkeypatch.setattr(retriever_module.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    monkeypatch.setattr(retriever_module, "embed", lambda _texts: [[0.0] * 1024])
    monkeypatch.setattr(retriever_module, "search_collection", lambda *_args, **_kwargs: [])
    retriever_module.invalidate_bm25_cache()

    collection = "kb"
    vector_store._ensure_collection_name(collection)
    old_chunk = Chunk(text="legacytoken", doc_name="old.md", chunk_index=0)
    new_chunk = Chunk(text="freshterm", doc_name="new.md", chunk_index=0)

    try:
        vector_store.add_to_collection(collection, [old_chunk], [[0.1] * 1024])
        before_delete = retriever_module.Retriever([], collection_name=collection)
        assert [c.doc_name for c in before_delete.retrieve("legacytoken")] == ["old.md"]

        vector_store.delete_doc(collection, "old.md")
        after_delete = retriever_module.Retriever([], collection_name=collection)
        assert after_delete.retrieve("legacytoken") == []

        vector_store.add_to_collection(collection, [new_chunk], [[0.2] * 1024])
        after_add = retriever_module.Retriever([], collection_name=collection)
        assert [c.doc_name for c in after_add.retrieve("freshterm")] == ["new.md"]
    finally:
        vector_store._client.close()
        vector_store._client = None
        retriever_module.invalidate_bm25_cache()
