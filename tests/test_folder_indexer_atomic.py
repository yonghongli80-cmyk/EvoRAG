"""Folder reindexing must not destroy the old collection before embeddings exist."""

from unittest.mock import patch

from qdrant_client import QdrantClient

from rag.models import Chunk


def test_index_folder_keeps_old_vectors_when_all_embeddings_fail(tmp_path, monkeypatch):
    import rag.vector_store as vector_store
    from rag.folder_indexer import index_folder

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "new.txt").write_text("new", encoding="utf-8")

    vector_store._client = QdrantClient(path=str(tmp_path / "qdrant"))
    monkeypatch.setattr(vector_store, "_invalidate_bm25", lambda _collection: None)
    old = Chunk(text="old content", doc_name="old.txt", chunk_index=0)
    vector_store.add([old], [[0.1] * 1024])

    try:
        with patch("rag.loader.load", return_value="new content"), patch(
            "rag.chunker.chunk",
            return_value=[Chunk(text="new content", doc_name="new.txt", chunk_index=0)],
        ), patch("rag.embedder.embed", side_effect=RuntimeError("embedding unavailable")):
            result = index_folder(str(docs))

        points, _ = vector_store._get_client().scroll(
            collection_name=vector_store.COLLECTION_NAME,
            limit=10,
            with_payload=True,
        )
        assert result["chunks"] == 0
        assert [point.payload["doc_name"] for point in points] == ["old.txt"]
    finally:
        vector_store._client.close()
        vector_store._client = None


def test_index_folder_skips_failed_file_and_indexes_successful_file(tmp_path):
    from rag.folder_indexer import index_folder

    (tmp_path / "bad.txt").write_text("bad", encoding="utf-8")
    (tmp_path / "good.txt").write_text("good", encoding="utf-8")

    def load_file(path):
        return "bad" if path.endswith("bad.txt") else "good"

    def chunk_file(text, doc_name):
        return [Chunk(text=text, doc_name=doc_name, chunk_index=0)]

    def embed_file(texts):
        if texts == ["bad"]:
            raise RuntimeError("bad embedding")
        return [[0.2] * 1024]

    with patch("rag.loader.load", side_effect=load_file), patch(
        "rag.chunker.chunk", side_effect=chunk_file
    ), patch("rag.embedder.embed", side_effect=embed_file), patch(
        "rag.vector_store.clear"
    ) as clear, patch("rag.vector_store.add") as add:
        result = index_folder(str(tmp_path))

    assert result["files"] == 1
    assert result["chunks"] == 1
    clear.assert_called_once_with()
    added_chunks, added_embeddings = add.call_args.args
    assert [chunk.doc_name for chunk in added_chunks] == ["good.txt"]
    assert added_embeddings == [[0.2] * 1024]
