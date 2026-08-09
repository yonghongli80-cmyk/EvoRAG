"""Task 1: material schema migration and preserved rows."""

import pytest

from rag.user_db import UserDB


@pytest.fixture
def legacy_version_db(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("policy.pdf", "rag_docs", user_id)
    db._conn.execute(
        "INSERT INTO document_versions "
        "(id, document_id, version_no, original_filename, file_path, content_hash, uploaded_by, status) "
        "VALUES (4, ?, 1, 'policy.pdf', 'p.pdf', 'h', ?, 'current')",
        (document_id, user_id),
    )
    db._conn.commit()
    return db


def test_material_schema_rebuilds_version_check_and_preserves_rows(legacy_version_db):
    legacy_version_db.create_material_schema()

    assert legacy_version_db.get_document_version_by_id(4)["status"] == "current"
    legacy_version_db.set_version_status(4, "recycle_pending")
    assert legacy_version_db.get_document_version_by_id(4)["status"] == "recycle_pending"
    assert legacy_version_db.foreign_key_check() == []


def test_material_schema_is_idempotent(legacy_version_db):
    legacy_version_db.create_material_schema()
    legacy_version_db.create_material_schema()
    assert legacy_version_db.get_document_version_by_id(4)["status"] == "current"


def test_material_schema_adds_logical_metadata_columns(legacy_version_db):
    legacy_version_db.create_material_schema()

    document = legacy_version_db.get_document_permission_by_id(1)
    assert "logical_path" in document
    assert "document_type" in document
    assert "metadata_version" in document
    assert "deleted_at" in document


def test_material_schema_creates_job_and_archive_tables(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()

    tables = {
        r["name"]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for name in (
        "document_index_jobs",
        "material_archive_manifests",
        "material_archive_entries",
        "material_organization_proposals",
        "material_organization_items",
        "material_organization_batches",
        "material_organization_batch_items",
        "material_search_fts",
    ):
        assert name in tables
