import pytest
from fastapi import HTTPException

from rag.api_security import require_kb_edit, require_kb_view


def test_acl_hidden_chunks_never_reach_jev_passage_request(monkeypatch):
    """ACL 隐藏的候选块不得出现在 Jev 候选判断请求中。"""
    from unittest.mock import MagicMock, patch

    from config import settings
    from rag.jev.models import PreflightDecision
    from rag.jev.policy import PassageSelection, RouteDecision
    from rag.models import Chunk
    from rag.pipeline import RAGPipeline

    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "kb1")

    visible = Chunk(
        text="visible", doc_name="visible.txt", chunk_index=0,
        document_id=1, document_version_id=11,
    )
    invisible = Chunk(
        text="private", doc_name="private.txt", chunk_index=0,
        document_id=2, document_version_id=22,
    )
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="rag",
        should_rewrite=False,
        reason="retrieval_confident",
        preflight=PreflightDecision(
            intent="retrieval_qa",
            intent_confidence=0.95,
            complexity="simple",
            complexity_confidence=0.9,
            rewrite_probability=0.1,
            model="jev-latest",
            question_set_version="preflight-v1",
            duration_ms=1.0,
        ),
    )
    jev.select.return_value = PassageSelection(
        accepted=(visible,), conflicts=(), excluded_ids=(), has_answer_evidence=True,
    )
    user = {"id": 7, "permission_level": 1, "is_admin": False}
    user_db = MagicMock()
    user_db.get_accessible_doc_names.return_value = ["visible.txt"]
    user_db.get_document_permission.return_value = {"id": 1, "current_version_id": 11}
    user_db.list_document_versions.return_value = []
    user_db.get_document_permissions_by_names.return_value = {"visible.txt": {"id": 1}}

    with patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"), \
         patch("rag.versioned_retrieval.retrieve_current_then_history", return_value=[visible, invisible]), \
         patch("rag.pipeline.generate", return_value="unexpected generated answer") as generate:
        pipeline = RAGPipeline(
            kb_id="kb1",
            user_db=user_db,
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
            jev_service=jev,
        )
        pipeline.query("内部制度", user=user, kb_id="kb1")

    sent = jev.select.call_args.args[1]
    assert [chunk.doc_name for chunk in sent] == ["visible.txt"]
    assert all("private" not in chunk.text for chunk in sent)
    generate.assert_not_called()
    pipeline.close()


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
