"""Task 10: existing-document version migration."""

from pathlib import Path

from rag.document_versions import migrate_existing_document_versions
from rag.user_db import UserDB


def legacy_document_fixture(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("policy.pdf", "rag_docs", user_id)
    upload_root = tmp_path / "upload"
    upload_root.mkdir()
    (upload_root / "policy.pdf").write_bytes(b"policy v1")
    versions_root = tmp_path / "versions"
    return db, upload_root, versions_root, document_id


def missing_legacy_document_fixture(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("policy.pdf", "rag_docs", user_id)
    return db, tmp_path / "upload", tmp_path / "versions", document_id


def test_existing_document_migration_is_idempotent(tmp_path):
    db, upload_root, versions_root, document_id = legacy_document_fixture(tmp_path)

    first = migrate_existing_document_versions(db, versions_root, upload_root)
    second = migrate_existing_document_versions(db, versions_root, upload_root)

    assert first.migrated == 1
    assert second.migrated == 0
    assert second.unchanged == 1
    assert len(db.list_document_versions(document_id)) == 1


def test_missing_legacy_source_becomes_failed_version(tmp_path):
    db, upload_root, versions_root, document_id = missing_legacy_document_fixture(tmp_path)

    report = migrate_existing_document_versions(db, versions_root, upload_root)

    assert report.missing == 1
    version = db.list_document_versions(document_id)[0]
    assert version["status"] == "failed"
    assert db.get_document_permission_by_id(document_id)["current_version_id"] is None


def test_migration_copies_legacy_source_without_moving(tmp_path):
    db, upload_root, versions_root, document_id = legacy_document_fixture(tmp_path)

    migrate_existing_document_versions(db, versions_root, upload_root)

    # The legacy source stays in place; the versioned copy is immutable.
    assert (upload_root / "policy.pdf").is_file()
    version = db.get_current_document_version(document_id)
    assert Path(version["file_path"]).is_file()
    assert Path(version["file_path"]).read_bytes() == b"policy v1"
