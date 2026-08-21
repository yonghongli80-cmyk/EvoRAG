from unittest.mock import MagicMock


def test_non_organization_user_cannot_create_personal_knowledge_base(
    isolated_api, make_user, monkeypatch
):
    import rag.api as api

    _user, headers = make_user("legacy-outsider")
    manager = MagicMock()
    monkeypatch.setattr(api, "KnowledgeBaseManager", lambda: manager)

    response = isolated_api.post(
        "/knowledge-bases",
        headers=headers,
        json={"name": "Personal fallback", "scope": "private"},
    )

    assert response.status_code == 404
    manager.create_kb.assert_not_called()


def test_tags_only_include_documents_visible_to_the_caller(
    isolated_api, make_user, monkeypatch
):
    import rag.vector_store as vector_store

    user, headers = make_user("tag-reader")
    db = isolated_api.app.state.user_db
    db.create_document_permission("visible.txt", "rag_docs", user["id"], scope="private")
    other, _ = make_user("tag-owner")
    db.create_document_permission("hidden.txt", "rag_docs", other["id"], scope="private")

    visible = MagicMock(payload={"doc_name": "visible.txt", "tags": ["allowed"]})
    hidden = MagicMock(payload={"doc_name": "hidden.txt", "tags": ["secret"]})
    client = MagicMock()
    client.collection_exists.return_value = True
    client.scroll.return_value = ([visible, hidden], None)
    monkeypatch.setattr(vector_store, "_get_client", lambda: client)

    response = isolated_api.get("/tags", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"tags": ["allowed"]}


def test_knowledge_gap_details_require_administrator(
    isolated_api, make_user, monkeypatch
):
    import rag.api as api

    _employee, employee_headers = make_user("gap-employee")
    _admin, admin_headers = make_user("gap-admin", is_admin=True)
    analyzer = MagicMock()
    analyzer.get_gaps.return_value = [{"question": "confidential question"}]
    monkeypatch.setattr(api, "get_gap_analyzer", lambda: analyzer)

    denied = isolated_api.get("/analytics/gaps", headers=employee_headers)
    allowed = isolated_api.get("/analytics/gaps", headers=admin_headers)

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json() == [{"question": "confidential question"}]
