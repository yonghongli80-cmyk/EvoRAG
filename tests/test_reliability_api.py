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
