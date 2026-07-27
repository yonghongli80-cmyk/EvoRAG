import pytest
from fastapi import HTTPException

from rag.api_security import require_kb_edit, require_kb_view


def test_shared_kb_is_visible_only_to_owner_admin_and_named_share(isolated_api, make_user):
    owner, _ = make_user("owner")
    shared, _ = make_user("shared")
    outsider, _ = make_user("outsider")
    admin, _ = make_user("admin", is_admin=True)
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_finance", "Finance", owner["id"], "shared")
    db.share_kb("kb_finance", shared["id"], owner["id"], "view")

    assert require_kb_view(db, "kb_finance", owner)["kb_id"] == "kb_finance"
    assert require_kb_view(db, "kb_finance", shared)["kb_id"] == "kb_finance"
    assert require_kb_view(db, "kb_finance", admin)["kb_id"] == "kb_finance"
    with pytest.raises(HTTPException) as exc:
        require_kb_view(db, "kb_finance", outsider)
    assert exc.value.status_code == 403


def test_view_share_cannot_edit_but_edit_share_can(isolated_api, make_user):
    owner, _ = make_user("owner")
    viewer, _ = make_user("viewer")
    editor, _ = make_user("editor")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_ops", "Ops", owner["id"], "shared")
    db.share_kb("kb_ops", viewer["id"], owner["id"], "view")
    db.share_kb("kb_ops", editor["id"], owner["id"], "edit")

    with pytest.raises(HTTPException) as exc:
        require_kb_edit(db, "kb_ops", viewer)
    assert exc.value.status_code == 403
    assert require_kb_edit(db, "kb_ops", editor)["kb_id"] == "kb_ops"


def test_missing_metadata_is_conflict_for_mutation(isolated_api, make_user):
    user, _ = make_user("alice")

    with pytest.raises(HTTPException) as exc:
        require_kb_edit(isolated_api.app.state.user_db, "orphan_collection", user)

    assert exc.value.status_code == 409


def test_shared_scope_is_consistent_across_list_detail_and_query(
    isolated_api, make_user, monkeypatch
):
    import rag.api as api
    from rag.knowledge_base import KnowledgeBaseInfo
    from rag.models import Chunk

    owner, _ = make_user("owner")
    shared, shared_headers = make_user("shared")
    outsider, outsider_headers = make_user("outsider")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_finance", "Finance", owner["id"], "shared")
    db.share_kb("kb_finance", shared["id"], owner["id"], "view")

    class FakeManager:
        def list_kbs(self):
            return [KnowledgeBaseInfo("kb_finance", "Finance", 1, "")]

        def search(self, kb_id, question, top_k):
            return [Chunk("allowed", "public.txt", 0)]

    monkeypatch.setattr(api, "KnowledgeBaseManager", FakeManager)

    assert isolated_api.get("/knowledge-bases", headers=outsider_headers).json() == []
    assert isolated_api.get("/knowledge-bases/kb_finance", headers=outsider_headers).status_code == 403
    assert isolated_api.post(
        "/knowledge-bases/kb_finance/query", headers=outsider_headers, json={"question": "budget"}
    ).status_code == 403
    assert isolated_api.get("/knowledge-bases/kb_finance", headers=shared_headers).status_code == 200
    assert isolated_api.post(
        "/knowledge-bases/kb_finance/query", headers=shared_headers, json={"question": "budget"}
    ).status_code == 200


def test_orphan_collection_delete_is_conflict(isolated_api, make_user, monkeypatch):
    import rag.api as api

    _, headers = make_user("alice")

    class FakeManager:
        def delete_kb(self, kb_id):
            raise AssertionError("authorization must fail before collection deletion")

    monkeypatch.setattr(api, "KnowledgeBaseManager", FakeManager)
    response = isolated_api.delete("/knowledge-bases/kb_orphan", headers=headers)

    assert response.status_code == 409
