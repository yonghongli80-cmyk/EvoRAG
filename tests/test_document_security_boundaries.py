import pytest
from fastapi import HTTPException

from rag.api_security import require_doc_edit, require_doc_view
from rag.models import Chunk
from rag.permissions import filter_chunks_by_permission


def test_historical_document_without_permission_is_admin_only(isolated_api, make_user):
    user, _ = make_user("alice")
    admin, _ = make_user("admin", is_admin=True)
    db = isolated_api.app.state.user_db

    with pytest.raises(HTTPException) as exc:
        require_doc_view(db, "legacy.pdf", "rag_docs", user)
    assert exc.value.status_code == 403
    assert require_doc_view(db, "legacy.pdf", "rag_docs", admin)["doc_name"] == "legacy.pdf"


def test_private_document_is_removed_from_public_kb_results(isolated_api, make_user):
    owner, _ = make_user("owner")
    reader, _ = make_user("reader")
    db = isolated_api.app.state.user_db
    db.create_document_permission("private.pdf", "kb_public", owner["id"], scope="private")
    db.create_document_permission("public.pdf", "kb_public", owner["id"], scope="public")
    chunks = [
        Chunk(text="private", doc_name="private.pdf", chunk_index=0),
        Chunk(text="public", doc_name="public.pdf", chunk_index=0),
        Chunk(text="legacy", doc_name="legacy.pdf", chunk_index=0),
    ]

    filtered = filter_chunks_by_permission(db, "kb_public", chunks, reader)

    assert [chunk.doc_name for chunk in filtered] == ["public.pdf"]


def test_document_share_persists_requested_edit_level(isolated_api, make_user):
    owner, owner_headers = make_user("owner")
    editor, _ = make_user("editor")
    db = isolated_api.app.state.user_db
    doc_id = db.create_document_permission("plan.pdf", "rag_docs", owner["id"])

    response = isolated_api.post(
        f"/documents/{doc_id}/share",
        headers=owner_headers,
        json={"user_id": editor["id"], "permission": "edit"},
    )

    assert response.status_code == 200
    assert db.is_document_shared(doc_id, editor["id"], permission="edit") is True


def test_protected_document_rejects_owner_and_admin_mutation(isolated_api, make_user):
    owner, _ = make_user("owner")
    admin, _ = make_user("admin", is_admin=True)
    db = isolated_api.app.state.user_db
    db.create_document_permission("policy.pdf", "rag_docs", owner["id"], protected=True, scope="public")

    for user in (owner, admin):
        with pytest.raises(HTTPException) as exc:
            require_doc_edit(db, "policy.pdf", "rag_docs", user)
        assert exc.value.status_code == 403
