from rag.reliability.comparison import ComparisonCitationDraft, DocumentEvidence


class _StaticComparisonTool:
    async def query_document(self, document_id, question, context):
        return DocumentEvidence(
            document_id=document_id,
            text="five days",
            citations=(
                ComparisonCitationDraft(
                    document_id=document_id,
                    document_version_id=11,
                    chunk_index=0,
                    section_title="Leave",
                    text_preview="five days",
                    rank=0,
                ),
            ),
        )


def test_compare_persists_result_before_returning_it(isolated_api, make_user, monkeypatch):
    owner, owner_headers = make_user("owner")
    document_id = isolated_api.app.state.user_db.create_document_permission(
        "policy.pdf", "rag_docs", owner["id"]
    )
    monkeypatch.setattr(
        isolated_api.app.state, "comparison_document_tool", _StaticComparisonTool(), raising=False
    )

    response = isolated_api.post(
        "/reliability/compare",
        headers=owner_headers,
        json={"question": "leave", "document_ids": [document_id], "dimensions": []},
    )

    assert response.status_code == 200
    assert response.json()["comparison_id"] > 0


def test_comparison_read_rechecks_deleted_source_without_leak(isolated_api, make_user, monkeypatch):
    owner, owner_headers = make_user("owner")
    document_id = isolated_api.app.state.user_db.create_document_permission(
        "policy.pdf", "rag_docs", owner["id"]
    )
    monkeypatch.setattr(
        isolated_api.app.state, "comparison_document_tool", _StaticComparisonTool(), raising=False
    )
    created = isolated_api.post(
        "/reliability/compare",
        headers=owner_headers,
        json={"question": "leave", "document_ids": [document_id], "dimensions": []},
    )
    readable = isolated_api.get(
        f"/reliability/comparisons/{created.json()['comparison_id']}", headers=owner_headers
    )
    assert readable.status_code == 200
    isolated_api.app.state.user_db.delete_document_permission(document_id)

    response = isolated_api.get(
        f"/reliability/comparisons/{created.json()['comparison_id']}", headers=owner_headers
    )

    assert response.status_code in {403, 404}
    assert "policy.pdf" not in response.text


def test_compare_uses_application_tool_factory_when_no_tool_is_injected(
    isolated_api, make_user, monkeypatch
):
    owner, owner_headers = make_user("owner")
    document_id = isolated_api.app.state.user_db.create_document_permission(
        "policy.pdf", "rag_docs", owner["id"]
    )
    monkeypatch.delattr(isolated_api.app.state, "comparison_document_tool", raising=False)
    monkeypatch.setattr(
        isolated_api.app.state,
        "comparison_document_tool_factory",
        lambda: _StaticComparisonTool(),
        raising=False,
    )

    response = isolated_api.post(
        "/reliability/compare",
        headers=owner_headers,
        json={"question": "leave", "document_ids": [document_id], "dimensions": []},
    )

    assert response.status_code == 200
    assert response.json()["comparison_id"] > 0


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


def test_compare_rejects_legacy_owner_after_kb_quarantine(isolated_api, make_user):
    owner, headers = make_user("legacy-owner")
    db = isolated_api.app.state.user_db
    document_id = db.create_document_permission("legacy.pdf", "legacy-kb", owner["id"])
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,kb_type) VALUES('legacy-kb','unassigned')"
        )

    response = isolated_api.post(
        "/reliability/compare",
        headers=headers,
        json={"question": "compare", "document_ids": [document_id], "dimensions": []},
    )

    assert response.status_code == 403


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
