"""Task 3: versioned ingest service and duplicate API contract."""

from pathlib import Path


def _upload(client, headers, filename, content, action=None):
    data = {}
    if action:
        data["duplicate_action"] = action
    return client.post(
        "/upload",
        headers=headers,
        data=data,
        files={"file": (filename, content, "text/plain")},
    )


def test_reject_duplicate_returns_structured_conflict(isolated_api, make_user, mock_embedding_indexing):
    _, headers = make_user("owner")
    first = _upload(isolated_api, headers, "policy.txt", b"v1")
    second = _upload(isolated_api, headers, "policy.txt", b"v2")

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "duplicate_document"
    assert second.json()["detail"]["current_version"] == 1


def test_overwrite_preserves_owner_and_old_version(isolated_api, make_user, mock_embedding_indexing):
    owner, owner_headers = make_user("owner")
    _upload(isolated_api, owner_headers, "policy.txt", b"v1")

    response = _upload(isolated_api, owner_headers, "policy.txt", b"v2", action="overwrite")

    db = isolated_api.app.state.user_db
    document = db.get_document_permission("policy.txt", "rag_docs")
    versions = db.list_document_versions(document["id"])
    assert response.status_code == 200
    assert document["owner_id"] == owner["id"]
    assert [(item["status"]) for item in versions] == ["current", "historical"]
    assert all(Path(item["file_path"]).is_file() for item in versions)


def test_keep_creates_numbered_copy(isolated_api, make_user, mock_embedding_indexing):
    _, headers = make_user("owner")
    _upload(isolated_api, headers, "policy.txt", b"v1")

    response = _upload(isolated_api, headers, "policy.txt", b"v2", action="keep")

    db = isolated_api.app.state.user_db
    assert response.status_code == 200
    assert response.json()["filename"] == "policy (1).txt"
    assert db.get_document_permission("policy (1).txt", "rag_docs") is not None


def test_identical_content_overwrite_is_unchanged(isolated_api, make_user, mock_embedding_indexing):
    _, headers = make_user("owner")
    first = _upload(isolated_api, headers, "policy.txt", b"same")
    document_id = first.json()["document_id"]

    second = _upload(isolated_api, headers, "policy.txt", b"same", action="overwrite")

    db = isolated_api.app.state.user_db
    assert second.status_code == 200
    assert second.json()["status"] == "unchanged"
    assert second.json()["version_no"] == first.json()["version_no"]
    assert len(db.list_document_versions(document_id)) == 1


def test_non_editor_cannot_overwrite(isolated_api, make_user, mock_embedding_indexing):
    _, owner_headers = make_user("owner")
    _upload(isolated_api, owner_headers, "policy.txt", b"v1")

    _, intruder_headers = make_user("intruder")
    response = _upload(isolated_api, intruder_headers, "policy.txt", b"v2", action="overwrite")

    assert response.status_code == 403


def test_failed_index_compensates_permission_and_version(isolated_api, make_user, monkeypatch, mock_embedding_indexing):
    _, headers = make_user("owner")

    def fail_index(*args, **kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(
        "rag.knowledge_base.KnowledgeBaseManager.add_document", fail_index
    )

    response = _upload(isolated_api, headers, "failed.txt", b"body")

    db = isolated_api.app.state.user_db
    assert response.status_code == 400
    assert db.get_document_permission("failed.txt", "rag_docs") is None
    assert list(Path(isolated_api.app.state.document_versions_root).rglob("*.*")) == []


def test_versions_endpoint_requires_view_and_redacts_uploader(isolated_api, make_user, mock_embedding_indexing):
    owner, owner_headers = make_user("owner")
    first = _upload(isolated_api, owner_headers, "policy.txt", b"v1")
    document_id = first.json()["document_id"]

    response = isolated_api.get(
        f"/documents/{document_id}/versions", headers=owner_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["versions"][0]["version_no"] == 1
    assert "file_path" not in response.text

    _, outsider_headers = make_user("outsider")
    denied = isolated_api.get(
        f"/documents/{document_id}/versions", headers=outsider_headers
    )
    assert denied.status_code in (403, 409)
