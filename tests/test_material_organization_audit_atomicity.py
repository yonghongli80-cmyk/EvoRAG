"""Audit only successful organization mutations, atomically with their SQL state."""

from __future__ import annotations

import sqlite3

import pytest


def _org_kb(db, kb_id: str, org_code: str = "ORG-A") -> None:
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES(?,?)", (kb_id, kb_id))
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES(?,?,'organization')",
            (kb_id, org_code),
        )


def _organization_fixture(db, make_user):
    admin, headers = make_user("org-audit-admin", is_admin=True)
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('ORG-A','Synthetic A','team')")
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('ORG-B','Synthetic B','team')")
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P-ADMIN',?,'Admin','Manager')",
            (admin["id"],),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P-ADMIN','ORG-A',1)")
        conn.execute("INSERT INTO org_memberships VALUES('P-ADMIN','ORG-B',0)")
        conn.execute("INSERT INTO org_manager_scopes VALUES('P-ADMIN','ORG-A')")
        conn.execute("INSERT INTO org_manager_scopes VALUES('P-ADMIN','ORG-B')")
    _org_kb(db, "kb-org-a", "ORG-A")
    _org_kb(db, "kb-org-b", "ORG-B")
    return admin, headers


def _document(db, owner_id: int, kb_id: str, name: str, path: str = "Inbox") -> int:
    doc_id = db.create_document_permission(name, kb_id, owner_id, 1, False)
    with db.locked_transaction() as conn:
        conn.execute("UPDATE document_permissions SET logical_path=? WHERE id=?", (path, doc_id))
    version_id = db.create_document_version(doc_id, name, f"/synthetic/{name}", f"hash-{name}", owner_id)
    db.activate_document_version(doc_id, version_id, kb_id, name)
    return doc_id


def _events(db, action: str) -> list[tuple[str, str, str]]:
    rows = db._conn.execute(
        "SELECT target_type,target_id,result FROM org_audit_events WHERE action=? ORDER BY id",
        (action,),
    ).fetchall()
    return [tuple(row) for row in rows]


def test_confirm_audits_only_applied_kbs_once_and_replay_adds_nothing(isolated_api, make_user, monkeypatch):
    import rag.materials.organizer as organizer

    client = isolated_api
    db = client.app.state.user_db
    admin, headers = _organization_fixture(db, make_user)
    first = _document(db, admin["id"], "kb-org-a", "first.txt")
    conflicted = _document(db, admin["id"], "kb-org-a", "conflicted.txt")
    second_kb = _document(db, admin["id"], "kb-org-b", "second-kb.txt")
    plan = {
        "items": [
            {"document_id": first, "target_path": "Ready/one", "confidence": 0.95},
            {"document_id": conflicted, "target_path": "Ready/conflict", "confidence": 0.95},
            {"document_id": second_kb, "target_path": "Ready/two", "confidence": 0.95},
        ]
    }
    monkeypatch.setattr(organizer, "generate_structured", lambda *_args: plan)
    preview = client.post("/materials/organization/preview", headers=headers, json={"text": "sort"})
    assert preview.status_code == 200, preview.text
    proposal_id = preview.json()["proposal_id"]
    item_ids = [item["id"] for item in preview.json()["items"]]

    with db.locked_transaction() as conn:
        conn.execute("UPDATE document_permissions SET deleted_at='synthetic' WHERE id=?", (conflicted,))

    payload = {"selected_item_ids": item_ids, "idempotency_key": "confirm-once"}
    confirmed = client.post(f"/materials/organization/{proposal_id}/confirm", headers=headers, json=payload)
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["status"] == "partial"
    assert [item["status"] for item in result["items"]] == ["applied", "permission_conflict", "applied"]
    assert db.get_document_permission_by_id(first)["logical_path"] == "Ready/one"
    assert db.get_document_permission_by_id(conflicted)["logical_path"] == "Inbox"
    assert db.get_document_permission_by_id(second_kb)["logical_path"] == "Ready/two"
    assert _events(db, "organization_confirmed") == [
        ("knowledge_base", "kb-org-a", "success"),
        ("knowledge_base", "kb-org-b", "success"),
    ]

    replay = client.post(f"/materials/organization/{proposal_id}/confirm", headers=headers, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["batch_id"] == result["batch_id"]
    assert _events(db, "organization_confirmed") == [
        ("knowledge_base", "kb-org-a", "success"),
        ("knowledge_base", "kb-org-b", "success"),
    ]


def test_rollback_audits_only_rows_restored_and_not_repeat_or_conflict(isolated_api, make_user, monkeypatch):
    import rag.materials.organizer as organizer

    client = isolated_api
    db = client.app.state.user_db
    admin, headers = _organization_fixture(db, make_user)
    first = _document(db, admin["id"], "kb-org-a", "rollback-one.txt")
    changed = _document(db, admin["id"], "kb-org-a", "rollback-changed.txt")
    monkeypatch.setattr(
        organizer,
        "generate_structured",
        lambda *_args: {
            "items": [
                {"document_id": first, "target_path": "Sorted/one", "confidence": 0.95},
                {"document_id": changed, "target_path": "Sorted/two", "confidence": 0.95},
            ]
        },
    )
    preview = client.post("/materials/organization/preview", headers=headers, json={"text": "sort"})
    proposal_id = preview.json()["proposal_id"]
    confirm = client.post(
        f"/materials/organization/{proposal_id}/confirm",
        headers=headers,
        json={"selected_item_ids": [item["id"] for item in preview.json()["items"]], "idempotency_key": "apply"},
    )
    assert confirm.status_code == 200, confirm.text
    batch_id = confirm.json()["batch_id"]

    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE document_permissions SET logical_path='Manual',metadata_version=metadata_version+1 WHERE id=?",
            (changed,),
        )
    rollback_url = f"/materials/organization/batches/{batch_id}/rollback"
    rolled = client.post(rollback_url, headers=headers, json={"idempotency_key": "undo"})
    assert rolled.status_code == 200, rolled.text
    assert [item["status"] for item in rolled.json()["items"]] == ["rolled_back", "metadata_conflict"]
    assert db.get_document_permission_by_id(first)["logical_path"] == "Inbox"
    assert db.get_document_permission_by_id(changed)["logical_path"] == "Manual"
    assert _events(db, "organization_rolled_back") == [("knowledge_base", "kb-org-a", "success")]

    repeated = client.post(rollback_url, headers=headers, json={"idempotency_key": "undo-again"})
    assert repeated.status_code == 200, repeated.text
    assert [item["status"] for item in repeated.json()["items"]] == ["rolled_back", "metadata_conflict"]
    assert _events(db, "organization_rolled_back") == [("knowledge_base", "kb-org-a", "success")]


def test_audit_insert_failure_rolls_back_confirm_metadata_and_batch(isolated_api, make_user, monkeypatch):
    import rag.materials.organizer as organizer

    client = isolated_api
    db = client.app.state.user_db
    admin, headers = _organization_fixture(db, make_user)
    document_id = _document(db, admin["id"], "kb-org-a", "audit-fails.txt")
    monkeypatch.setattr(
        organizer,
        "generate_structured",
        lambda *_args: {"items": [{"document_id": document_id, "target_path": "Sorted", "confidence": 0.95}]},
    )
    preview = client.post("/materials/organization/preview", headers=headers, json={"text": "sort"})
    proposal_id = preview.json()["proposal_id"]
    with db.locked_transaction() as conn:
        conn.execute(
            "CREATE TRIGGER reject_organization_audit BEFORE INSERT ON org_audit_events "
            "WHEN NEW.action='organization_confirmed' BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        client.post(
            f"/materials/organization/{proposal_id}/confirm",
            headers=headers,
            json={"selected_item_ids": [preview.json()["items"][0]["id"]], "idempotency_key": "atomic"},
        )

    assert db.get_document_permission_by_id(document_id)["logical_path"] == "Inbox"
    assert db._conn.execute("SELECT COUNT(*) FROM material_organization_batches").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM material_organization_batch_items").fetchone()[0] == 0
    assert _events(db, "organization_confirmed") == []


def test_rollback_audit_failure_preserves_applied_state_and_retry_can_succeed(isolated_api, make_user, monkeypatch):
    import rag.materials.organizer as organizer

    client = isolated_api
    db = client.app.state.user_db
    admin, headers = _organization_fixture(db, make_user)
    document_id = _document(db, admin["id"], "kb-org-a", "rollback-audit-fails.txt")
    monkeypatch.setattr(
        organizer,
        "generate_structured",
        lambda *_args: {"items": [{"document_id": document_id, "target_path": "Sorted", "confidence": 0.95}]},
    )
    preview = client.post("/materials/organization/preview", headers=headers, json={"text": "sort"})
    proposal_id = preview.json()["proposal_id"]
    confirmed = client.post(
        f"/materials/organization/{proposal_id}/confirm",
        headers=headers,
        json={"selected_item_ids": [preview.json()["items"][0]["id"]], "idempotency_key": "apply"},
    )
    assert confirmed.status_code == 200, confirmed.text
    batch_id = confirmed.json()["batch_id"]
    rollback_url = f"/materials/organization/batches/{batch_id}/rollback"
    with db.locked_transaction() as conn:
        conn.execute(
            "CREATE TRIGGER reject_organization_rollback_audit BEFORE INSERT ON org_audit_events "
            "WHEN NEW.action='organization_rolled_back' BEGIN SELECT RAISE(ABORT,'synthetic rollback audit failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic rollback audit failure"):
        client.post(rollback_url, headers=headers, json={"idempotency_key": "undo-first"})

    assert db.get_document_permission_by_id(document_id)["logical_path"] == "Sorted"
    batch_item = db._conn.execute(
        "SELECT status FROM material_organization_batch_items WHERE batch_id=?", (batch_id,)
    ).fetchone()
    assert batch_item["status"] == "applied"
    assert _events(db, "organization_rolled_back") == []

    with db.locked_transaction() as conn:
        conn.execute("DROP TRIGGER reject_organization_rollback_audit")
    retried = client.post(rollback_url, headers=headers, json={"idempotency_key": "undo-retry"})
    assert retried.status_code == 200, retried.text
    assert retried.json()["items"][0]["status"] == "rolled_back"
    assert db.get_document_permission_by_id(document_id)["logical_path"] == "Inbox"
    assert _events(db, "organization_rolled_back") == [("knowledge_base", "kb-org-a", "success")]

    repeated = client.post(rollback_url, headers=headers, json={"idempotency_key": "undo-repeat"})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["items"][0]["status"] == "rolled_back"
    assert _events(db, "organization_rolled_back") == [("knowledge_base", "kb-org-a", "success")]
