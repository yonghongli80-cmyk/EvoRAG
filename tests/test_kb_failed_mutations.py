from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import rag.api as api
from rag.auth import create_token


@pytest.fixture
def kb_client(monkeypatch):
    """Use the autouse sandbox DB without entering the app lifespan."""
    monkeypatch.setattr("config.settings.auth_enabled", True)
    db = api.app.state.user_db
    client = TestClient(api.app)
    return client, db


def _owner(db, username="kb-owner"):
    user_id = db.create_user(username, "Password-123!")
    user = db.get_user_by_id(user_id)
    token = create_token({"user_id": user_id, "username": username, "security_version": 1})
    return user, {"Authorization": f"Bearer {token}"}


def _existing_document(db, owner, kb_id, name, versions_root, kb_root):
    db.create_kb_metadata(kb_id, kb_id, owner["id"], "private")
    document_id = db.create_document_permission(name, kb_id, owner["id"])
    paths = []
    for version_no in (1, 2):
        path = versions_root / str(document_id) / str(version_no) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"version {version_no}", encoding="utf-8")
        version_id = db.create_document_version(
            document_id, name, str(path), f"hash-{version_no}", owner["id"]
        )
        paths.append(path)
    db.activate_document_version(document_id, version_id, kb_id, name)
    kb_document_id = db.create_kb_document_pending(kb_id, name)
    db.finish_kb_document(kb_document_id, str(paths[-1]), "[]", "old summary", 1)
    legacy = kb_root / kb_id / str(kb_document_id) / name
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy", encoding="utf-8")
    return kb_document_id, paths, legacy


@pytest.mark.parametrize("entry", ["upload", "link"])
def test_failed_overwrite_preserves_preexisting_kb_directory_row(
    kb_client, monkeypatch, tmp_path, entry
):
    client, db = kb_client
    owner, headers = _owner(db)
    versions_root = api.app.state.document_versions_root
    kb_root = api.app.state.kb_files_root
    kb_id = "kb_overwrite"
    kb_document_id, _paths, _legacy = _existing_document(
        db, owner, kb_id, "report.txt", versions_root, kb_root
    )
    source_id = db.create_document_permission("source.txt", "rag_docs", owner["id"])
    source_path = tmp_path / "source.txt"
    source_path.write_text("replacement", encoding="utf-8")
    source_version_id = db.create_document_version(
        source_id, "source.txt", str(source_path), "source-hash", owner["id"]
    )
    db.activate_document_version(source_id, source_version_id, "rag_docs", "source.txt")
    monkeypatch.setattr(
        "rag.routers.knowledge.ingest_document_version",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected ingest failure")),
    )

    if entry == "upload":
        response = client.post(
            f"/knowledge-bases/{kb_id}/documents",
            headers=headers,
            data={"duplicate_action": "overwrite"},
            files={"file": ("report.txt", b"replacement", "text/plain")},
        )
    else:
        response = client.post(
            f"/knowledge-bases/{kb_id}/documents/link",
            headers=headers,
            json={"document_id": source_id, "duplicate_action": "overwrite"},
        )

    assert response.status_code == 400
    row = db.get_kb_document(kb_id, "report.txt")
    assert row["id"] == kb_document_id
    assert row["summary"] == "old summary"


def test_duplicate_reject_keeps_existing_kb_directory_row(kb_client):
    client, db = kb_client
    owner, headers = _owner(db)
    kb_id = "kb_reject_control"
    kb_document_id, _paths, _legacy = _existing_document(
        db, owner, kb_id, "report.txt", api.app.state.document_versions_root,
        api.app.state.kb_files_root,
    )

    response = client.post(
        f"/knowledge-bases/{kb_id}/documents",
        headers=headers,
        files={"file": ("report.txt", b"body", "text/plain")},
    )

    assert response.status_code == 409
    assert db.get_kb_document(kb_id, "report.txt")["id"] == kb_document_id


def test_delete_document_removes_every_version_and_legacy_path(
    kb_client, monkeypatch
):
    client, db = kb_client
    owner, headers = _owner(db)
    kb_id = "kb_delete_versions"
    _kb_document_id, version_paths, legacy_path = _existing_document(
        db, owner, kb_id, "report.txt", api.app.state.document_versions_root,
        api.app.state.kb_files_root,
    )
    other_path = api.app.state.document_versions_root / "999" / "1" / "other.txt"
    other_path.parent.mkdir(parents=True)
    other_path.write_text("keep", encoding="utf-8")
    outside_path = other_path.parent.parent.parent.parent / "outside.txt"
    outside_path.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        "rag.routers.knowledge.KnowledgeBaseManager.remove_document",
        lambda *args, **kwargs: None,
    )

    response = client.delete(
        f"/knowledge-bases/{kb_id}/documents/report.txt", headers=headers
    )

    assert response.status_code == 200
    assert all(not path.exists() for path in version_paths)
    assert other_path.read_text(encoding="utf-8") == "keep"
    assert outside_path.read_text(encoding="utf-8") == "keep"
    assert db.get_document_permission("report.txt", kb_id) is None


def test_delete_kb_removes_all_versions_but_keeps_other_kb_files(
    kb_client, monkeypatch
):
    client, db = kb_client
    owner, headers = _owner(db)
    versions_root = api.app.state.document_versions_root
    kb_root = api.app.state.kb_files_root
    kb_id = "kb_delete_all_versions"
    _kb_document_id, paths, _legacy = _existing_document(
        db, owner, kb_id, "report.txt", versions_root, kb_root
    )
    other_kb_id = "kb_keep_versions"
    _other_document_id, other_paths, _other_legacy = _existing_document(
        db, owner, other_kb_id, "keep.txt", versions_root, kb_root
    )

    class Manager:
        def remove_document(self, *_args):
            return None

        def delete_kb(self, _kb_id):
            return None

    monkeypatch.setattr("rag.routers.knowledge.KnowledgeBaseManager", Manager)
    response = client.delete(f"/knowledge-bases/{kb_id}", headers=headers)

    assert response.status_code == 200
    assert all(not path.exists() for path in paths)
    assert all(path.exists() for path in other_paths)
    assert db.get_document_permission("report.txt", kb_id) is None
    assert db.get_document_permission("keep.txt", other_kb_id) is not None


@pytest.mark.parametrize("failure", ["unlink", "escape"])
def test_failed_or_unsafe_cleanup_preserves_retry_identity(
    kb_client, monkeypatch, failure
):
    client, db = kb_client
    owner, headers = _owner(db)
    kb_id = "kb_cleanup_retry"
    kb_document_id, version_paths, _legacy = _existing_document(
        db, owner, kb_id, "report.txt", api.app.state.document_versions_root,
        api.app.state.kb_files_root,
    )
    if failure == "unlink":
        original_unlink = Path.unlink

        def fail_version_unlink(path, *args, **kwargs):
            if path == version_paths[0]:
                raise OSError("injected unlink failure")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_version_unlink)
    else:
        with db.locked_transaction() as conn:
            conn.execute(
                "UPDATE document_versions SET file_path = ? WHERE id = ("
                "SELECT MIN(id) FROM document_versions WHERE document_id = ?)",
                (str(Path(version_paths[0]).parent.parent.parent / ".." / "outside.txt"),
                 db.get_document_permission("report.txt", kb_id)["id"]),
            )
    response = client.delete(
        f"/knowledge-bases/{kb_id}/documents/report.txt", headers=headers
    )

    assert response.status_code == 400
    assert db.get_kb_document(kb_id, "report.txt")["id"] == kb_document_id
    assert db.get_document_permission("report.txt", kb_id) is not None
    assert len(db.list_document_versions(db.get_document_permission("report.txt", kb_id)["id"])) == 2
