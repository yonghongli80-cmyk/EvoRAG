from pathlib import Path


def test_anonymous_upload_is_rejected_before_body_is_persisted(isolated_api):
    response = isolated_api.post(
        "/upload",
        files={"file": ("secret.txt", b"private text", "text/plain")},
    )

    assert response.status_code == 401
    assert list(Path(isolated_api.app.state.upload_dir).glob("*")) == []


def test_existing_filename_returns_conflict_without_overwrite(isolated_api, make_user):
    _, headers = make_user("alice")
    upload_dir = isolated_api.app.state.upload_dir
    upload_dir.mkdir()
    existing = upload_dir / "same.txt"
    existing.write_bytes(b"old")

    response = isolated_api.post(
        "/upload",
        headers=headers,
        files={"file": ("same.txt", b"new", "text/plain")},
    )

    assert response.status_code == 409
    assert existing.read_bytes() == b"old"


def test_upload_directory_is_not_publicly_mounted(isolated_api):
    secret = isolated_api.app.state.upload_dir / "mounted-secret.txt"
    secret.parent.mkdir()
    secret.write_text("must not leak", encoding="utf-8")

    response = isolated_api.get("/data/upload/mounted-secret.txt")

    assert response.status_code == 404
    assert "must not leak" not in response.text


def test_all_ingest_routes_authenticate_before_parsing(isolated_api):
    upload = isolated_api.post(
        "/upload",
        files={"file": ("secret.txt", b"private", "text/plain")},
    )
    index = isolated_api.post(
        "/index",
        files={"file": ("secret.txt", b"private", "text/plain")},
    )
    batch = isolated_api.post(
        "/batch-import",
        files={"file": ("secret.csv", b"question,answer\nq,a", "text/csv")},
    )
    index_all = isolated_api.post("/index-all")

    assert [upload.status_code, index.status_code, batch.status_code, index_all.status_code] == [
        401,
        401,
        401,
        401,
    ]


def test_index_failure_compensates_file_and_permission(
    isolated_api, make_user, monkeypatch
):
    _, headers = make_user("alice")
    monkeypatch.setattr("rag.api.load_document", lambda path: "valid")

    def fail_index(*args, **kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(
        "rag.knowledge_base.KnowledgeBaseManager.add_document",
        fail_index,
    )

    response = isolated_api.post(
        "/upload",
        headers=headers,
        files={"file": ("failed.txt", b"private", "text/plain")},
    )

    assert response.status_code == 400
    assert list(isolated_api.app.state.upload_dir.glob("*")) == []
    assert (
        isolated_api.app.state.user_db.get_document_permission("failed.txt", "rag_docs")
        is None
    )


def test_delete_file_refreshes_the_default_pipeline(
    isolated_api, make_user, monkeypatch
):
    """Deleting a default-library file must rebuild its in-memory pipeline."""
    import rag.api as api

    user, headers = make_user("alice")
    upload_dir = isolated_api.app.state.upload_dir
    upload_dir.mkdir()
    (upload_dir / "delete-me.txt").write_text("private", encoding="utf-8")
    api.user_db.create_document_permission("delete-me.txt", "rag_docs", user["id"], 1, False)
    refreshed = object()
    monkeypatch.setattr("rag.vector_store.delete_doc", lambda *args: None)
    monkeypatch.setattr("rag.pipeline.RAGPipeline", lambda **kwargs: refreshed)

    response = isolated_api.delete("/files/delete-me.txt", headers=headers)

    assert response.status_code == 200
    assert api.pipeline is refreshed
