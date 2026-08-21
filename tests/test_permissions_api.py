"""Tests for permission-related API endpoints."""

import pytest

from rag.user_db import UserDB


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "test.db")
    udb = UserDB(path)
    yield udb
    udb.close()


def test_create_user_has_is_admin(db):
    """Verify users have is_admin after creation."""
    uid = db.create_user("alice", "s3cret")
    user = db.get_user_by_id(uid)
    assert "is_admin" in user
    assert user["is_admin"] is False
    assert "permission_level" not in user


def test_document_permission_crud(db):
    """Full CRUD for document_permissions."""
    uid = db.create_user("alice", "s3cret")
    doc_id = db.create_document_permission("test.pdf", "kb1", uid, is_public=False)
    assert doc_id > 0

    perm = db.get_document_permission("test.pdf", "kb1")
    assert perm["owner_id"] == uid
    assert perm["is_public"] is False
    assert perm["protected"] is False

    # Toggle visibility
    new_val = db.toggle_document_visibility(doc_id)
    assert new_val is True
    perm = db.get_document_permission_by_id(doc_id)
    assert perm["is_public"] is True

    db.delete_document_permission(doc_id)
    assert db.get_document_permission_by_id(doc_id) is None
