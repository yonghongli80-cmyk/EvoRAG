from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from rag.models import Chunk


def test_add_to_collection_replaces_same_document_without_duplicate_chunks(tmp_path, monkeypatch):
    import rag.vector_store as vector_store

    client = QdrantClient(path=str(tmp_path / "qdrant"))
    vector_store._client = client
    monkeypatch.setattr(vector_store, "_invalidate_bm25", lambda _collection: None)
    client.create_collection(
        collection_name="kb_test",
        vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
    )
    first = [
        Chunk(text="old zero", doc_name="guide.txt", chunk_index=0),
        Chunk(text="old one", doc_name="guide.txt", chunk_index=1),
    ]
    replacement = [Chunk(text="new zero", doc_name="guide.txt", chunk_index=0)]

    try:
        vector_store.add_to_collection("kb_test", first, [[0.1] * 1024, [0.2] * 1024])
        vector_store.add_to_collection("kb_test", replacement, [[0.3] * 1024])

        points, _ = client.scroll(
            collection_name="kb_test", limit=10, with_payload=True
        )
        assert [(point.payload["chunk_index"], point.payload["text"]) for point in points] == [
            (0, "new zero")
        ]
    finally:
        client.close()
        vector_store._client = None
