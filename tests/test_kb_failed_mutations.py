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
    if db.get_kb_metadata(kb_id) is None:
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


def test_document_delete_rejects_version_path_owned_by_another_document(
    kb_client, monkeypatch
):
    client, db = kb_client
    owner, headers = _owner(db)
    versions_root = api.app.state.document_versions_root
    kb_root = api.app.state.kb_files_root
    kb_id = "kb_version_owner_boundary"
    first_id, first_paths, _first_legacy = _existing_document(
        db, owner, kb_id, "first.txt", versions_root, kb_root
    )
    second_id, second_paths, _second_legacy = _existing_document(
        db, owner, kb_id, "second.txt", versions_root, kb_root
    )
    first_permission = db.get_document_permission("first.txt", kb_id)
    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE document_versions SET file_path = ? WHERE id = ("
            "SELECT MIN(id) FROM document_versions WHERE document_id = ?)",
            (str(second_paths[0]), first_permission["id"]),
        )
    manager_calls = []
    monkeypatch.setattr(
        "rag.routers.knowledge.KnowledgeBaseManager.remove_document",
        lambda *_args: manager_calls.append("removed"),
    )

    response = client.delete(
        f"/knowledge-bases/{kb_id}/documents/first.txt", headers=headers
    )

    assert response.status_code == 400
    assert manager_calls == []
    assert all(path.exists() for path in first_paths + second_paths)
    assert db.get_kb_document(kb_id, "first.txt")["id"] == first_id
    assert db.get_kb_document(kb_id, "second.txt")["id"] == second_id
    assert db.get_document_permission("first.txt", kb_id) is not None


def test_document_delete_rejects_version_symlink_into_another_document(
    kb_client, monkeypatch
):
    client, db = kb_client
    owner, headers = _owner(db)
    versions_root = api.app.state.document_versions_root
    kb_id = "kb_version_symlink_boundary"
    first_id, first_paths, _first_legacy = _existing_document(
        db, owner, kb_id, "first.txt", versions_root, api.app.state.kb_files_root
    )
    _second_id, second_paths, _second_legacy = _existing_document(
        db, owner, kb_id, "second.txt", versions_root, api.app.state.kb_files_root
    )
    symlink = versions_root / str(db.get_document_permission("first.txt", kb_id)["id"]) / "3" / "link.txt"
    symlink.parent.mkdir(parents=True)
    try:
        symlink.symlink_to(second_paths[0])
    except OSError as exc:
        pytest.skip(f"host cannot create a temporary symlink: {exc}")
    first_permission = db.get_document_permission("first.txt", kb_id)
    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE document_versions SET file_path = ? WHERE id = ("
            "SELECT MIN(id) FROM document_versions WHERE document_id = ?)",
            (str(symlink), first_permission["id"]),
        )
    monkeypatch.setattr(
        "rag.routers.knowledge.KnowledgeBaseManager.remove_document",
        lambda *_args: None,
    )

    response = client.delete(
        f"/knowledge-bases/{kb_id}/documents/first.txt", headers=headers
    )

    assert response.status_code == 400
    assert all(path.exists() for path in first_paths + second_paths)
    assert db.get_kb_document(kb_id, "first.txt")["id"] == first_id


@pytest.mark.parametrize("target", ["other_kb", "sibling_document"])
def test_document_delete_rejects_legacy_path_owned_by_another_record(
    kb_client, monkeypatch, target
):
    client, db = kb_client
    owner, headers = _owner(db)
    versions_root = api.app.state.document_versions_root
    kb_root = api.app.state.kb_files_root
    kb_id = "kb_legacy_owner_boundary"
    first_id, first_paths, _first_legacy = _existing_document(
        db, owner, kb_id, "first.txt", versions_root, kb_root
    )
    victim_kb = "kb_victim" if target == "other_kb" else kb_id
    victim_name = "victim.txt" if target == "other_kb" else "sibling.txt"
    victim_id, _victim_versions, victim_legacy = _existing_document(
        db, owner, victim_kb, victim_name, versions_root, kb_root
    )
    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE kb_documents SET file_path = ? WHERE id = ?",
            (str(victim_legacy), first_id),
        )
    manager_calls = []
    monkeypatch.setattr(
        "rag.routers.knowledge.KnowledgeBaseManager.remove_document",
        lambda *_args: manager_calls.append("removed"),
    )

    response = client.delete(
        f"/knowledge-bases/{kb_id}/documents/first.txt", headers=headers
    )

    assert response.status_code == 400
    assert manager_calls == []
    assert all(path.exists() for path in first_paths)
    assert victim_legacy.exists()
    assert db.get_kb_document(kb_id, "first.txt") is not None
    assert db.get_kb_document(victim_kb, victim_name)["id"] == victim_id


def test_delete_kb_prevalidates_every_document_owner_before_side_effects(
    kb_client, monkeypatch
):
    client, db = kb_client
    owner, headers = _owner(db)
    versions_root = api.app.state.document_versions_root
    kb_root = api.app.state.kb_files_root
    kb_id = "kb_bulk_owner_boundary"
    first_id, first_paths, _first_legacy = _existing_document(
        db, owner, kb_id, "first.txt", versions_root, kb_root
    )
    second_id, second_paths, _second_legacy = _existing_document(
        db, owner, kb_id, "second.txt", versions_root, kb_root
    )
    first_permission = db.get_document_permission("first.txt", kb_id)
    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE document_versions SET file_path = ? WHERE id = ("
            "SELECT MIN(id) FROM document_versions WHERE document_id = ?)",
            (str(second_paths[0]), first_permission["id"]),
        )
    manager_calls = []

    class Manager:
        def remove_document(self, *_args):
            manager_calls.append("document")

        def delete_kb(self, _kb_id):
            manager_calls.append("collection")

    monkeypatch.setattr("rag.routers.knowledge.KnowledgeBaseManager", Manager)
    response = client.delete(f"/knowledge-bases/{kb_id}", headers=headers)

    assert response.status_code == 503
    assert db.get_kb_lifecycle(kb_id)["state"] == "deleting"
    assert manager_calls == []
    assert all(path.exists() for path in first_paths + second_paths)
    assert db.get_document_permission("first.txt", kb_id) is not None
    assert db.get_document_permission("second.txt", kb_id) is not None
    assert db.get_kb_document(kb_id, "first.txt")["id"] == first_id
    assert db.get_kb_document(kb_id, "second.txt")["id"] == second_id


def test_document_delete_removes_real_legacy_path_and_keeps_siblings(
    kb_client, monkeypatch
):
    client, db = kb_client
    owner, headers = _owner(db)
    versions_root = api.app.state.document_versions_root
    kb_root = api.app.state.kb_files_root
    kb_id = "kb_legacy_delete"
    document_id, version_paths, legacy_path = _existing_document(
        db, owner, kb_id, "legacy.txt", versions_root, kb_root
    )
    sibling_id, _sibling_versions, sibling_legacy = _existing_document(
        db, owner, kb_id, "sibling.txt", versions_root, kb_root
    )
    _other_document_id, _other_versions, other_legacy = _existing_document(
        db, owner, "kb_legacy_other", "other.txt", versions_root, kb_root
    )
    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE kb_documents SET file_path = ? WHERE id = ?",
            (str(legacy_path), document_id),
        )
    monkeypatch.setattr(
        "rag.routers.knowledge.KnowledgeBaseManager.remove_document",
        lambda *_args: None,
    )

    response = client.delete(
        f"/knowledge-bases/{kb_id}/documents/legacy.txt", headers=headers
    )

    assert response.status_code == 200
    assert not legacy_path.exists()
    assert all(not path.exists() for path in version_paths)
    assert sibling_legacy.exists()
    assert other_legacy.exists()
    assert db.get_kb_document(kb_id, "sibling.txt")["id"] == sibling_id
    assert db.get_kb_document("kb_legacy_other", "other.txt") is not None


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
