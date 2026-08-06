"""Task 7: knowledge operations API authorization and summary."""


def test_summary_returns_counts(isolated_api, make_user):
    _, headers = make_user("alice")

    response = isolated_api.get(
        "/knowledge-operations/summary", headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    for key in ("open", "overdue", "outdated", "not_useful", "stale_knowledge_bases"):
        assert key in body


def test_change_document_owner_requires_admin(isolated_api, make_user):
    _, user_headers = make_user("alice")
    _, admin_headers = make_user("admin", is_admin=True)
    db = isolated_api.app.state.user_db
    alice_id = db.get_user_by_username("alice")["id"]
    doc_id = db.create_document_permission("policy.pdf", "rag_docs", alice_id)

    denied = isolated_api.put(
        f"/documents/{doc_id}/owner",
        headers=user_headers,
        json={"owner_id": alice_id},
    )
    assert denied.status_code == 403

    allowed = isolated_api.put(
        f"/documents/{doc_id}/owner",
        headers=admin_headers,
        json={"owner_id": alice_id},
    )
    assert allowed.status_code == 200
    assert db.get_document_permission_by_id(doc_id)["owner_id"] == alice_id


def test_admin_task_listing_respects_status_filter(isolated_api, make_user):
    _, admin_headers = make_user("admin", is_admin=True)

    response = isolated_api.get(
        "/knowledge-operations/tasks",
        headers=admin_headers,
        params={"status": "open", "page_size": 50},
    )

    assert response.status_code == 200
    assert "items" in response.json()
    assert all(item["status"] == "open" for item in response.json()["items"])


def test_admin_reassigns_task_assignee(isolated_api, make_user, monkeypatch):
    import rag.knowledge_tasks

    owner_id = isolated_api.app.state.user_db.create_user("owner", "Password-123!")
    doc_id = isolated_api.app.state.user_db.create_document_permission(
        "policy.pdf", "rag_docs", owner_id
    )
    version_id = isolated_api.app.state.user_db.create_document_version(
        doc_id, "policy.pdf", "p.pdf", "h", owner_id
    )
    isolated_api.app.state.user_db.activate_document_version(
        doc_id, version_id, "rag_docs", "policy.pdf"
    )
    task_id = isolated_api.app.state.user_db.upsert_active_task(
        task_type="outdated",
        cluster_key=f"outdated:{doc_id}",
        active_key=f"outdated:{doc_id}",
        document_id=doc_id,
        document_version_id=version_id,
        assigned_to=owner_id,
        report_count=1,
    )
    _, admin_headers = make_user("admin", is_admin=True)

    response = isolated_api.put(
        f"/knowledge-operations/tasks/{task_id}/assignee",
        headers=admin_headers,
        json={"assignee_id": owner_id},
    )

    assert response.status_code == 200
    assert response.json()["assigned_to"] == owner_id
