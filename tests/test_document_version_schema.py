"""Task 1: document version schema, atomic activation, and validity tests."""

import pytest

from rag.user_db import UserDB, VersionConflict


def make_versioned_document(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("policy.pdf", "rag_docs", user_id)
    version_id = db.create_document_version(
        document_id, "policy.pdf", str(tmp_path / "v1.pdf"), "hash-1", user_id
    )
    db.activate_document_version(document_id, version_id, "rag_docs", "policy.pdf")
    return db, user_id, document_id, version_id


def test_new_document_version_can_be_activated_atomically(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("policy.pdf", "rag_docs", user_id)
    version_id = db.create_document_version(
        document_id=document_id,
        original_filename="policy.pdf",
        file_path=str(tmp_path / "v1" / "policy.pdf"),
        content_hash="abc123",
        uploaded_by=user_id,
    )

    db.activate_document_version(document_id, version_id, "rag_docs", "policy.pdf")

    current = db.get_current_document_version(document_id)
    assert current["id"] == version_id
    assert current["version_no"] == 1
    assert current["status"] == "current"
    assert current["validity_status"] == "valid"
    assert db.get_document_permission_by_id(document_id)["current_version_id"] == version_id


def test_activating_second_version_makes_first_historical(tmp_path):
    db, user_id, document_id, first_id = make_versioned_document(tmp_path)
    second_id = db.create_document_version(
        document_id, "policy.pdf", str(tmp_path / "v2.pdf"), "hash-2", user_id
    )

    db.activate_document_version(document_id, second_id, "rag_docs", "policy.pdf")

    versions = db.list_document_versions(document_id)
    assert [(item["version_no"], item["status"]) for item in versions] == [
        (2, "current"),
        (1, "historical"),
    ]


def test_activating_non_staging_version_raises_conflict(tmp_path):
    db, user_id, document_id, first_id = make_versioned_document(tmp_path)
    db.close()

    db2 = UserDB(str(tmp_path / "users.db"))
    second_id = db2.create_document_version(
        document_id, "policy.pdf", str(tmp_path / "v2.pdf"), "hash-2", user_id
    )
    db2.activate_document_version(document_id, second_id, "rag_docs", "policy.pdf")

    # Re-activating an already-activated version must raise a version conflict.
    with pytest.raises(VersionConflict):
        db2.activate_document_version(document_id, second_id, "rag_docs", "policy.pdf")


def test_set_version_validity_persists_outdated_status(tmp_path):
    db, user_id, document_id, first_id = make_versioned_document(tmp_path)

    db.set_version_validity(first_id, "confirmed_outdated")

    current = db.get_current_document_version(document_id)
    assert current["validity_status"] == "confirmed_outdated"
