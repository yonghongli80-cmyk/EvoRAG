def test_compare_rejects_invisible_document(isolated_api, make_user):
    owner, owner_headers = make_user("owner")
    invisible_owner, _ = make_user("invisible-owner")
    visible_id = isolated_api.app.state.user_db.create_document_permission(
        "visible.pdf", "rag_docs", owner["id"]
    )
    invisible_id = isolated_api.app.state.user_db.create_document_permission(
        "invisible.pdf", "rag_docs", invisible_owner["id"]
    )

    response = isolated_api.post(
        "/reliability/compare",
        headers=owner_headers,
        json={
            "question": "比较制度",
            "document_ids": [visible_id, invisible_id],
            "dimensions": [],
        },
    )

    assert response.status_code == 403
    assert "invisible.pdf" not in response.text


def test_answer_reliability_returns_only_the_owners_persisted_fields(isolated_api, make_user):
    owner, owner_headers = make_user("owner")
    conversation_id = isolated_api.app.state.user_db.create_conversation(owner["id"])
    message_id = isolated_api.app.state.user_db.add_message(
        conversation_id,
        "assistant",
        "年假为五天",
        confidence_level="medium",
        confidence_reasons=("单一当前来源",),
        answerability_kind="answerable",
    )

    response = isolated_api.get(f"/answers/{message_id}/reliability", headers=owner_headers)

    assert response.status_code == 200
    assert response.json() == {
        "confidence_level": "medium",
        "confidence_reasons": ["单一当前来源"],
        "answerability_kind": "answerable",
    }


def test_admin_can_resolve_conflict_without_document_details(isolated_api, make_user):
    admin, admin_headers = make_user("admin", is_admin=True)
    owner, _ = make_user("owner")
    document_id = isolated_api.app.state.user_db.create_document_permission(
        "policy.pdf", "rag_docs", owner["id"]
    )
    conflict = isolated_api.app.state.user_db.create_knowledge_conflict(
        active_key="policy-conflict",
        subject_key="policy",
        attribute_key="days",
        unit_key="days",
        scope_key="all",
        citation_ids=(1,),
        document_ids=(document_id,),
        document_version_ids=(1,),
    )

    response = isolated_api.put(
        f"/knowledge-operations/conflicts/{conflict['id']}/resolve",
        headers=admin_headers,
        json={"resolution": "resolved", "note": "confirmed"},
    )

    assert response.status_code == 200
    assert response.json() == {"id": conflict["id"], "status": "resolved"}
