from __future__ import annotations

from pathlib import Path

import pytest

from rag.materials.recycle import PURGED_STATE, purge_document
from rag.spreadsheets.store import resolve_dataset_path
from rag.user_db import UserDB

PURGE_TIME = 100 + 31 * 86400


class RecoveringVectorStore:
    def __init__(self, fail=False):
        self.fail = fail
        self.deleted = []

    def delete_document(self, document_id):
        self.deleted.append(document_id)
        if self.fail:
            raise RuntimeError("vector store unavailable")


@pytest.fixture
def purge_fixture(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    user_id = db.create_user("alice", "Password-123!")
    document_id = db.create_document_permission("contract.pdf", "kb-1", user_id)
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    file_path = versions_root / str(document_id) / "v1.pdf"
    file_path.parent.mkdir()
    file_path.write_bytes(b"contract")
    version_id = db.create_document_version(
        document_id, "contract.pdf", str(file_path), "hash-1", user_id
    )
    db.set_version_file_path(version_id, str(file_path))
    db.activate_document_version(document_id, version_id, "kb-1", "contract.pdf")
    datasets_root = tmp_path / "structured_datasets"
    dataset_path = resolve_dataset_path(datasets_root, document_id, version_id)
    dataset_path.parent.mkdir(parents=True)
    dataset_path.write_bytes(b"sheet rows")
    db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    with db._lock, db.transaction(immediate=True):
        db._conn.execute(
            "UPDATE document_permissions SET deleted_at=1, restore_until=2 WHERE id=?",
            (document_id,),
        )
    yield db, document_id, file_path, dataset_path, datasets_root
    db.close()


def test_failed_file_unlink_keeps_purge_retryable(purge_fixture, monkeypatch):
    db, document_id, file_path, dataset_path, datasets_root = purge_fixture
    real_unlink = Path.unlink

    def deny_target(self, *args, **kwargs):
        if self == file_path:
            raise PermissionError("temporarily locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_target)
    with pytest.raises(PermissionError, match="temporarily locked"):
        purge_document(
            db, document_id, now=PURGE_TIME, structured_datasets_root=datasets_root
        )

    doc = db.get_document_permission_by_id(document_id)
    version = db.list_document_versions(document_id)[0]
    assert doc["purge_state"] != PURGED_STATE
    assert version["status"] == "current"
    assert file_path.is_file()
    assert dataset_path.is_file()
    assert db.get_structured_dataset_for_version(version["id"]) is not None

    monkeypatch.setattr(Path, "unlink", real_unlink)
    result = purge_document(
        db, document_id, now=PURGE_TIME + 1, structured_datasets_root=datasets_root
    )

    assert result.status == "purged"
    assert not file_path.exists()
    assert not dataset_path.exists()
    assert db.get_structured_dataset_for_version(version["id"]) is None
    assert db.get_document_permission_by_id(document_id)["purge_state"] == PURGED_STATE


def test_vector_cleanup_error_does_not_mark_document_purged(purge_fixture):
    db, document_id, _file_path, _dataset_path, datasets_root = purge_fixture
    vector_store = RecoveringVectorStore(fail=True)

    with pytest.raises(RuntimeError, match="vector store unavailable"):
        purge_document(
            db,
            document_id,
            now=PURGE_TIME,
            vector_store=vector_store,
            structured_datasets_root=datasets_root,
        )

    assert db.get_document_permission_by_id(document_id)["purge_state"] != PURGED_STATE

    vector_store.fail = False
    result = purge_document(
        db,
        document_id,
        now=PURGE_TIME + 1,
        vector_store=vector_store,
        structured_datasets_root=datasets_root,
    )

    assert result.status == "purged"
    assert db.get_document_permission_by_id(document_id)["purge_state"] == PURGED_STATE
