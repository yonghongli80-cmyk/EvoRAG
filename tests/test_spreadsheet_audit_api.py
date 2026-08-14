def _seed_audit(client, owner):
    db = client.app.state.user_db
    document_id = db.create_document_permission(
        "public-prices.xlsx", "rag_docs", owner["id"], scope="public"
    )
    base_version_id = db.create_document_version(
        document_id, "public-prices.xlsx", "v1.xlsx", "hash-1", owner["id"]
    )
    db.activate_document_version(
        document_id, base_version_id, "rag_docs", "public-prices.xlsx"
    )
    session_id, _ = db.reserve_spreadsheet_edit(
        document_id=document_id,
        base_version_id=base_version_id,
        actor_user_id=owner["id"],
        original_filename="public-prices.xlsx",
        staged_path="v2.xlsx",
        content_hash="hash-2",
        operation="edit",
        reason="price correction",
        restored_from_version_id=None,
        risk_flags=("public_document_edit",),
        changes=(
            {
                "sheet_key": "sheet_0001",
                "sheet_name_snapshot": "Prices",
                "cell_address": "B2",
                "old_value_type": "number",
                "old_value_json": "6800",
                "new_value_type": "number",
                "new_value_json": "6500",
                "sensitive": 0,
            },
        ),
    )
    return document_id, session_id


def test_admin_and_owner_can_list_and_read_grouped_audit(
    isolated_api, make_user
):
    owner, owner_headers = make_user("audit-owner")
    admin, admin_headers = make_user("audit-admin", is_admin=True)
    document_id, session_id = _seed_audit(isolated_api, owner)

    for headers in (owner_headers, admin_headers):
        response = isolated_api.get(
            f"/admin/spreadsheet-changes?document_id={document_id}&status=pending",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["session_id"] == session_id
        assert response.json()["items"][0]["risk_flags"] == [
            "public_document_edit"
        ]

    detail = isolated_api.get(
        f"/admin/spreadsheet-changes/{session_id}", headers=owner_headers
    )
    assert detail.status_code == 200
    assert detail.json()["sheets"] == [
        {
            "sheet_key": "sheet_0001",
            "sheet_name": "Prices",
            "changes": [
                {
                    "cell": "B2",
                    "old_value_type": "number",
                    "old_value": 6800,
                    "new_value_type": "number",
                    "new_value": 6500,
                }
            ],
        }
    ]
    assert admin["id"] != owner["id"]


def test_shared_editor_cannot_read_management_audit(isolated_api, make_user):
    owner, _ = make_user("audit-owner-deny")
    editor, editor_headers = make_user("audit-shared-editor")
    document_id, session_id = _seed_audit(isolated_api, owner)
    db = isolated_api.app.state.user_db
    with db._lock:
        db._conn.execute(
            "UPDATE document_permissions SET scope='shared' WHERE id=?",
            (document_id,),
        )
        db._conn.commit()
    db.share_document(document_id, editor["id"], owner["id"], "edit")

    assert isolated_api.get(
        "/admin/spreadsheet-changes", headers=editor_headers
    ).status_code == 403
    assert isolated_api.get(
        f"/admin/spreadsheet-changes/{session_id}", headers=editor_headers
    ).status_code == 404


def test_audit_page_size_is_bounded(isolated_api, make_user):
    _, admin_headers = make_user("audit-bounds", is_admin=True)
    assert isolated_api.get(
        "/admin/spreadsheet-changes?limit=101", headers=admin_headers
    ).status_code == 422


def test_structure_change_is_visible_in_audit_list_and_detail(
    isolated_api, make_user
):
    owner, headers = make_user("structure-audit-owner")
    db = isolated_api.app.state.user_db
    document_id = db.create_document_permission(
        "prices.xlsx", "rag_docs", owner["id"]
    )
    base_version_id = db.create_document_version(
        document_id, "prices.xlsx", "v1.xlsx", "hash-1", owner["id"]
    )
    db.activate_document_version(
        document_id, base_version_id, "rag_docs", "prices.xlsx"
    )
    session_id, _ = db.reserve_spreadsheet_structure_edit(
        document_id=document_id,
        base_version_id=base_version_id,
        actor_user_id=owner["id"],
        original_filename="prices.xlsx",
        staged_path="v2.xlsx",
        content_hash="hash-2",
        reason="make room",
        structure_change={
            "action": "insert_rows",
            "sheet_key": "sheet_0001",
            "sheet_name_snapshot": "Prices",
            "target_index": 2,
            "count": 3,
            "new_sheet_name": None,
            "affected_nonempty_cells": 0,
            "confirm_nonempty": 0,
        },
    )

    listing = isolated_api.get(
        f"/admin/spreadsheet-changes?document_id={document_id}",
        headers=headers,
    ).json()
    assert listing["items"][0]["structure_action"] == "insert_rows"

    detail = isolated_api.get(
        f"/admin/spreadsheet-changes/{session_id}", headers=headers
    ).json()
    assert detail["structure_change"] == {
        "action": "insert_rows",
        "sheet_key": "sheet_0001",
        "sheet_name": "Prices",
        "index": 2,
        "count": 3,
        "new_sheet_name": None,
        "affected_nonempty_cells": 0,
        "confirm_nonempty": False,
    }
