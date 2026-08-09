"""Task 7: tombstone, restore, and idempotent purge."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from rag.api_security import require_doc_view
from rag.materials.recycle import (
    RecycleError,
    delete_to_recycle,
    purge_document,
    restore_from_recycle,
)
from rag.user_db import UserDB

PDF = b"%PDF-1.4\ncontract"


class FakeVectorStore:
    def __init__(self):
        self._points = {}

    def add(self, document_id):
        self._points.setdefault(document_id, []).append("point")

    def points(self, document_id):
        return list(self._points.get(document_id, []))

    def delete_document(self, document_id):
        self._points.pop(document_id, None)


@pytest.fixture
def recycle_fixture(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    user_id = db.create_user("alice", "Password-123!")
    doc_id = db.create_document_permission("contract.pdf", "kb-1", user_id)
    versions_root = tmp_path / "versions"
    versions_root.mkdir()

    version_file = versions_root / "v1.pdf"
    version_id = db.create_document_version(
        doc_id, "contract.pdf", str(version_file), "hash-1", user_id
    )
    db.set_version_file_path(version_id, str(version_file))
    version_file.write_bytes(PDF)
    db.activate_document_version(doc_id, version_id, "kb-1", "contract.pdf")

    db.insert_material_search_fts(doc_id, version_id, "contract.pdf", "", "", "[]", "合同正文")

    conversation_id = db.create_conversation(user_id, "c")
    message_id = db.add_message(conversation_id, "assistant", "answer")
    with db._lock, db.transaction(immediate=True):
        db._conn.execute(
            "INSERT INTO answer_citations "
            "(assistant_message_id, document_id, document_version_id, chunk_index, section_title, text_preview, rank) "
            "VALUES (?, ?, ?, 0, '', '合同正文预览', 1)",
            (message_id, doc_id, version_id),
        )

    vector_store = FakeVectorStore()
    vector_store.add(doc_id)

    fixture = SimpleNamespace()
    fixture.db = db
    fixture.doc_id = doc_id
    fixture.version_id = version_id
    fixture.versions_root = versions_root
    fixture.vector_store = vector_store
    fixture.actor = {"id": user_id, "username": "alice", "is_admin": False}

    def open_doc(document_id):
        doc = db.get_document_permission_by_id(document_id)
        if doc is None:
            return SimpleNamespace(status_code=404)
        try:
            require_doc_view(db, doc["doc_name"], doc["kb_id"], fixture.actor)
            return SimpleNamespace(status_code=200)
        except HTTPException as exc:
            return SimpleNamespace(status_code=exc.status_code)

    fixture.open = open_doc
    fixture.download = open_doc

    def deliver_notification(document_id):
        doc = db.get_document_permission_by_id(document_id)
        if doc is not None and doc.get("deleted_at") is not None:
            return {"created": 0}
        return {"created": 1}

    fixture.deliver_notification = deliver_notification

    def search(text):
        return db.search_material_search_fts(text)

    fixture.search = search

    def current_version_id(document_id):
        return db.current_version_id(document_id)

    fixture.current_version_id = current_version_id

    def acl(document_id):
        doc = db.get_document_permission_by_id(document_id)
        return {k: doc.get(k) for k in ("owner_id", "scope", "is_public", "permission_level")}

    fixture.acl = acl

    def citation_state(document_id):
        return db.get_citation_state(document_id)

    fixture.citation_state = citation_state

    def version_file_exists(document_id):
        version = db.get_current_document_version(document_id)
        if version is None:
            return False
        return __import__("pathlib").Path(version["file_path"]).exists()

    fixture.version_file_exists = version_file_exists

    fixture.vector_points = vector_store.points
    fixture.delete = lambda document_id, actor, now: delete_to_recycle(
        db, document_id, actor, now
    )
    fixture.restore = lambda document_id, actor, expected, now: restore_from_recycle(
        db, document_id, actor, expected, now
    )
    fixture.purge = lambda document_id, now: purge_document(
        db, document_id, now, vector_store=vector_store, versions_root=versions_root
    )

    def snapshot(document_id):
        return SimpleNamespace(
            current_version_id=current_version_id(document_id),
            acl=acl(document_id),
        )

    fixture.snapshot = snapshot
    return fixture


def test_delete_denies_search_open_download_and_notification_immediately(recycle_fixture):
    f = recycle_fixture
    record = f.delete(f.doc_id, f.actor, now=100)
    assert record.status == "deleted"
    assert f.search("合同正文") == []
    assert f.open(f.doc_id).status_code == 404
    assert f.download(f.doc_id).status_code == 404
    assert f.deliver_notification(f.doc_id)["created"] == 0


def test_restore_before_thirty_days_preserves_version_and_acl(recycle_fixture):
    f = recycle_fixture
    before = f.snapshot(f.doc_id)
    f.delete(f.doc_id, f.actor, now=100)
    restored = f.restore(f.doc_id, f.actor, expected=2, now=100 + 29 * 86400)
    assert restored.status == "restored"
    assert f.current_version_id(f.doc_id) == before.current_version_id
    assert f.acl(f.doc_id) == before.acl
    assert f.search("合同正文") != []


def test_restore_is_denied_after_thirty_days_and_to_non_owner(recycle_fixture):
    f = recycle_fixture
    f.delete(f.doc_id, f.actor, now=100)
    with pytest.raises(RecycleError):
        f.restore(f.doc_id, f.actor, expected=2, now=100 + 31 * 86400)
    stranger = {"id": 999, "username": "mallory", "is_admin": False}
    with pytest.raises(RecycleError):
        f.restore(f.doc_id, stranger, expected=2, now=100 + 1 * 86400)


def test_purge_is_idempotent_and_keeps_minimal_citation_audit(recycle_fixture):
    f = recycle_fixture
    f.delete(f.doc_id, f.actor, now=100)
    first = f.purge(f.doc_id, now=100 + 31 * 86400)
    second = f.purge(f.doc_id, now=100 + 32 * 86400)
    assert first.removed_bytes > 0
    assert second.removed_bytes == 0
    assert f.citation_state(f.doc_id) == "purged_by_retention_policy"
    assert f.version_file_exists(f.doc_id) is False
    assert f.vector_points(f.doc_id) == []
    # The citation row survives with no text preview.
    row = f.db._conn.execute(
        "SELECT state, text_preview FROM answer_citations WHERE document_id = ?", (f.doc_id,)
    ).fetchone()
    assert row["state"] == "purged_by_retention_policy"
    assert row["text_preview"] == ""


def test_purge_requires_tombstone_deadline(recycle_fixture):
    f = recycle_fixture
    with pytest.raises(RecycleError):
        f.purge(f.doc_id, now=100)
