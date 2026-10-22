import hashlib

from openpyxl import Workbook

from rag.document_version_store import final_version_path
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset


def _seed_editable_spreadsheet(client, owner):
    db = client.app.state.user_db
    document_id = db.create_document_permission(
        "prices.xlsx", "rag_docs", owner["id"]
    )
    root = client.app.state.document_versions_root
    source = final_version_path(root, document_id, 1, "prices.xlsx")
    source.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.active.title = "Prices"
    book.active.append(["product", "price"])
    book.active.append(["Phone", 6800])
    book.save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    version_id = db.create_document_version(
        document_id, "prices.xlsx", str(source), digest, owner["id"]
    )
    dataset_id = db.create_structured_dataset(
        document_id, version_id, "xlsx", "row_groups"
    )
    target = resolve_dataset_path(
        client.app.state.structured_datasets_root, document_id, version_id
    )
    write_dataset(target, parse_spreadsheet(source, "xlsx", None))
    db.activate_document_and_dataset(
        document_id=document_id,
        version_id=version_id,
        dataset_id=dataset_id,
        kb_id="rag_docs",
        filename="prices.xlsx",
    )
    return document_id, version_id


def _payload(version_id, value=6500):
    return {
        "base_version_id": version_id,
        "changes": [
            {
                "sheet_key": "sheet_0001",
                "cell": "B2",
                "value_type": "number",
                "value": value,
            }
        ],
        "reason": "price update",
    }


def test_edit_endpoint_auth_validation_and_conflict_codes(isolated_api, make_user):
    owner, owner_headers = make_user("owner")
    document_id, version_id = _seed_editable_spreadsheet(isolated_api, owner)
    _, outsider_headers = make_user("outsider")

    assert isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        json=_payload(version_id),
    ).status_code == 401
    assert isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        headers=outsider_headers,
        json=_payload(version_id),
    ).status_code == 403
    assert isolated_api.post(
        "/documents/99999/spreadsheet-edits",
        headers=owner_headers,
        json=_payload(version_id),
    ).status_code == 404
    assert isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        headers=owner_headers,
        json=_payload(99999),
    ).status_code == 409
    invalid = _payload(version_id)
    invalid["changes"][0]["value"] = "6500"
    assert isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        headers=owner_headers,
        json=invalid,
    ).status_code == 422


def test_edit_endpoint_returns_unchanged_200_without_internal_values(
    isolated_api, make_user
):
    owner, headers = make_user("owner")
    document_id, version_id = _seed_editable_spreadsheet(isolated_api, owner)

    response = isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        headers=headers,
        json=_payload(version_id, value=6800),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "unchanged"}
    assert "file_path" not in response.text
    assert "6800" not in response.text


def test_edit_endpoint_returns_pending_and_protects_status_lookup(
    isolated_api, make_user
):
    owner, owner_headers = make_user("owner")
    document_id, version_id = _seed_editable_spreadsheet(isolated_api, owner)
    _, outsider_headers = make_user("outsider")

    response = isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        headers=owner_headers,
        json=_payload(version_id),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    status = isolated_api.get(
        f"/documents/{document_id}/spreadsheet-edits/{body['session_id']}",
        headers=owner_headers,
    )
    assert status.status_code == 200
    assert status.json() == {
        "session_id": body["session_id"],
        "version_id": body["version_id"],
        "status": "pending",
        "error_code": "",
    }
    denied = isolated_api.get(
        f"/documents/{document_id}/spreadsheet-edits/{body['session_id']}",
        headers=outsider_headers,
    )
    assert denied.status_code == 404
    assert "6500" not in response.text + status.text
    assert "document_versions" not in response.text + status.text


def test_second_edit_at_the_same_base_conflicts_while_one_is_pending(
    isolated_api, make_user
):
    owner, headers = make_user("owner")
    document_id, version_id = _seed_editable_spreadsheet(isolated_api, owner)

    accepted = isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        headers=headers,
        json=_payload(version_id, value=6500),
    )
    conflicted = isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        headers=headers,
        json=_payload(version_id, value=6400),
    )

    assert accepted.status_code == 202
    assert conflicted.status_code == 409
    assert conflicted.json()["detail"]["code"] == (
        "spreadsheet_pending_session_conflict"
    )
    db = isolated_api.app.state.user_db
    pending = db._conn.execute(
        "SELECT status FROM spreadsheet_edit_sessions WHERE document_id = ?",
        (document_id,),
    ).fetchall()
    assert [row["status"] for row in pending] == ["pending"]


def test_edit_endpoint_accepts_500_changes_and_rejects_501(
    isolated_api, make_user
):
    owner, headers = make_user("owner")
    document_id, version_id = _seed_editable_spreadsheet(isolated_api, owner)
    changes = [
        {
            "sheet_key": "sheet_0001",
            "cell": f"C{row}",
            "value_type": "number",
            "value": row,
        }
        for row in range(1, 502)
    ]

    rejected = isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        headers=headers,
        json={"base_version_id": version_id, "changes": changes},
    )
    accepted = isolated_api.post(
        f"/documents/{document_id}/spreadsheet-edits",
        headers=headers,
        json={"base_version_id": version_id, "changes": changes[:500]},
    )

    assert rejected.status_code == 422
    assert accepted.status_code == 202


def test_structure_edit_endpoint_creates_pending_version_and_requires_confirmation(
    isolated_api, make_user
):
    owner, headers = make_user("owner")
    document_id, version_id = _seed_editable_spreadsheet(isolated_api, owner)

    rejected = isolated_api.post(
        f"/documents/{document_id}/spreadsheet-structure-edits",
        headers=headers,
        json={
            "base_version_id": version_id,
            "action": "delete_rows",
            "sheet_key": "sheet_0001",
            "index": 2,
        },
    )
    accepted = isolated_api.post(
        f"/documents/{document_id}/spreadsheet-structure-edits",
        headers=headers,
        json={
            "base_version_id": version_id,
            "action": "insert_rows",
            "sheet_key": "sheet_0001",
            "index": 2,
            "count": 1,
            "reason": "make room",
        },
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == (
        "nonempty_delete_requires_confirmation"
    )
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "pending"
