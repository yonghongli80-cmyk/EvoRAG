from qdrant_client import QdrantClient

from rag.models import Chunk


def test_versioned_first_write_creates_only_the_default_collection(tmp_path, monkeypatch):
    import rag.vector_store as vector_store

    client = QdrantClient(path=str(tmp_path / "qdrant"))
    vector_store._client = client
    monkeypatch.setattr(vector_store.settings, "embed_dimension", 4)
    monkeypatch.setattr(vector_store, "_invalidate_bm25", lambda _collection: None)

    try:
        assert not client.collection_exists(vector_store.COLLECTION_NAME)
        vector_store.add_to_collection(
            vector_store.COLLECTION_NAME,
            [Chunk("first body", "first.txt", 0, document_id=1, document_version_id=1, version_no=1)],
            [[0.1, 0.2, 0.3, 0.4]],
            document_id=1,
            document_version_id=1,
            version_no=1,
        )
        assert client.collection_exists(vector_store.COLLECTION_NAME)
        assert client.count(vector_store.COLLECTION_NAME).count == 1

        try:
            vector_store.add_to_collection("kb_missing", [], [])
        except ValueError as error:
            assert "不存在" in str(error)
        else:
            raise AssertionError("unknown custom KB must not be created implicitly")
        assert not client.collection_exists("kb_missing")
    finally:
        client.close()
        vector_store._client = None


def test_upload_first_version_and_keep_use_real_local_qdrant(
    isolated_api, make_user, tmp_path, monkeypatch
):
    import rag.embedder
    import rag.vector_store as vector_store

    owner, headers = make_user("first_default_upload")
    client = QdrantClient(path=str(tmp_path / "upload-qdrant"))
    vector_store._client = client
    monkeypatch.setattr(
        rag.embedder,
        "embed",
        lambda texts: [[0.1] * vector_store.settings.embed_dimension for _ in texts],
    )
    assert not client.collection_exists(vector_store.COLLECTION_NAME)

    def upload(name: str, body: bytes, action: str | None = None):
        data = {"duplicate_action": action} if action else {}
        return isolated_api.post(
            "/upload",
            headers=headers,
            data=data,
            files={"file": (name, body, "text/plain")},
        )

    try:
        first = upload("first.txt", b"first version content")
        assert first.status_code == 200, first.text
        assert client.collection_exists(vector_store.COLLECTION_NAME)
        assert client.count(vector_store.COLLECTION_NAME).count > 0

        duplicate = upload("first.txt", b"second version content")
        assert duplicate.status_code == 409

        kept = upload("first.txt", b"second version content", "keep")
        assert kept.status_code == 200, kept.text
        assert kept.json()["filename"] == "first (1).txt"
        db = isolated_api.app.state.user_db
        assert db.get_document_permission("first.txt", "rag_docs") is not None
        assert db.get_document_permission("first (1).txt", "rag_docs") is not None
        assert client.count(vector_store.COLLECTION_NAME).count >= 2
    finally:
        client.close()
        vector_store._client = None
