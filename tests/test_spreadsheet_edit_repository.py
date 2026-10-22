import sqlite3

import pytest

from rag.user_db import UserDB, VersionConflict


@pytest.fixture
def edit_db(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("editor", "Password-123!")
    document_id = db.create_document_permission(
        "prices.xlsx", "rag_docs", user_id
    )
    version_id = db.create_document_version(
        document_id,
        "prices.xlsx",
        "versions/1/prices.xlsx",
        "old-hash",
        user_id,
    )
    db.activate_document_version(
        document_id, version_id, "rag_docs", "prices.xlsx"
    )
    yield db, user_id, document_id, version_id
    db.close()


def _change(cell_address: str = "B12") -> dict[str, object]:
    return {
        "sheet_key": "sheet_0001",
        "cell_address": cell_address,
        "old_value_type": "number",
        "old_value_json": "6800",
        "new_value_type": "number",
        "new_value_json": "6500",
        "sheet_name_snapshot": "Prices",
        "sensitive": 0,
    }


def _reserve(db, user_id, document_id, base_version_id, changes):
    return db.reserve_spreadsheet_edit(
        document_id=document_id,
        base_version_id=base_version_id,
        actor_user_id=user_id,
        original_filename="prices.xlsx",
        staged_path="staging/pending.xlsx",
        content_hash="new-hash",
        operation="edit",
        reason="price update",
        restored_from_version_id=None,
        changes=changes,
    )


def test_reservation_writes_version_session_and_cell_changes_atomically(edit_db):
    db, user_id, document_id, current_version_id = edit_db

    session_id, version_id = _reserve(
        db, user_id, document_id, current_version_id, (_change(),)
    )

    session = db.get_spreadsheet_edit_session(session_id)
    assert session is not None
    assert session["base_version_id"] == current_version_id
    assert session["new_version_id"] == version_id
    assert session["status"] == "pending"
    assert session["change_count"] == 1
    version = db.get_document_version_by_id(version_id)
    assert version is not None
    assert version["status"] == "staging"
    stored = db._conn.execute(
        "SELECT sheet_key, cell_address, old_value_json, new_value_json "
        "FROM spreadsheet_cell_changes WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert tuple(stored) == ("sheet_0001", "B12", "6800", "6500")


def test_stale_base_rolls_back_without_creating_version_or_session(edit_db):
    db, user_id, document_id, _ = edit_db
    versions_before = db._conn.execute(
        "SELECT COUNT(*) FROM document_versions"
    ).fetchone()[0]

    with pytest.raises(VersionConflict, match="spreadsheet_base_version_conflict"):
        _reserve(db, user_id, document_id, 99999, (_change(),))

    assert db._conn.execute(
        "SELECT COUNT(*) FROM document_versions"
    ).fetchone()[0] == versions_before
    assert db._conn.execute(
        "SELECT COUNT(*) FROM spreadsheet_edit_sessions"
    ).fetchone()[0] == 0


def test_duplicate_cell_rolls_back_the_whole_reservation(edit_db):
    db, user_id, document_id, current_version_id = edit_db
    versions_before = db._conn.execute(
        "SELECT COUNT(*) FROM document_versions"
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        _reserve(
            db,
            user_id,
            document_id,
            current_version_id,
            (_change(), _change()),
        )

    assert db._conn.execute(
        "SELECT COUNT(*) FROM document_versions"
    ).fetchone()[0] == versions_before
    assert db._conn.execute(
        "SELECT COUNT(*) FROM spreadsheet_edit_sessions"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM spreadsheet_cell_changes"
    ).fetchone()[0] == 0


def test_pending_session_blocks_a_second_reservation_at_the_same_base(edit_db):
    db, user_id, document_id, current_version_id = edit_db
    first_session, first_version = _reserve(
        db, user_id, document_id, current_version_id, (_change(),)
    )
    versions_before = db._conn.execute(
        "SELECT COUNT(*) FROM document_versions"
    ).fetchone()[0]

    with pytest.raises(VersionConflict, match="spreadsheet_pending_session_conflict"):
        _reserve(db, user_id, document_id, current_version_id, (_change("B13"),))

    # The whole reservation rolls back: no orphan version, session, or changes.
    assert db._conn.execute(
        "SELECT COUNT(*) FROM document_versions"
    ).fetchone()[0] == versions_before
    assert db._conn.execute(
        "SELECT COUNT(*) FROM spreadsheet_edit_sessions"
    ).fetchone()[0] == 1
    assert db.get_spreadsheet_edit_session(first_session)["status"] == "pending"
    assert db.get_document_version_by_id(first_version)["status"] == "staging"

    # Once the pending session is resolved the same base may be reserved again.
    db.set_spreadsheet_edit_status(first_session, "cancelled")
    second_session, _ = _reserve(
        db, user_id, document_id, current_version_id, (_change("B13"),)
    )
    assert second_session != first_session


def test_pending_session_only_blocks_its_own_document(edit_db):
    db, user_id, document_id, current_version_id = edit_db
    _reserve(db, user_id, document_id, current_version_id, (_change(),))
    other_document = db.create_document_permission(
        "other.xlsx", "rag_docs", user_id
    )
    other_version = db.create_document_version(
        other_document, "other.xlsx", "versions/2/other.xlsx", "hash-2", user_id
    )
    db.activate_document_version(
        other_document, other_version, "rag_docs", "other.xlsx"
    )

    session_id, _ = _reserve(
        db, user_id, other_document, other_version, (_change(),)
    )

    assert db.get_spreadsheet_edit_session(session_id)["status"] == "pending"


def test_active_session_cannot_transition_back_to_pending(edit_db):
    db, user_id, document_id, current_version_id = edit_db
    session_id, _ = _reserve(
        db, user_id, document_id, current_version_id, (_change(),)
    )
    db.set_spreadsheet_edit_status(session_id, "active", activated_at=123.0)

    with pytest.raises(VersionConflict, match="spreadsheet_edit_status_conflict"):
        db.set_spreadsheet_edit_status(session_id, "pending")

    session = db.get_spreadsheet_edit_session(session_id)
    assert session is not None
    assert session["status"] == "active"
    assert session["activated_at"] == 123.0


def test_structure_reservation_is_atomic_and_keeps_operation_details(edit_db):
    db, user_id, document_id, current_version_id = edit_db

    session_id, version_id = db.reserve_spreadsheet_structure_edit(
        document_id=document_id,
        base_version_id=current_version_id,
        actor_user_id=user_id,
        original_filename="prices.xlsx",
        staged_path="staging/structure.xlsx",
        content_hash="structure-hash",
        reason="add August",
        structure_change={
            "action": "add_sheet",
            "sheet_key": None,
            "sheet_name_snapshot": None,
            "target_index": None,
            "count": 1,
            "new_sheet_name": "August",
            "affected_nonempty_cells": 0,
            "confirm_nonempty": 0,
        },
    )

    session = db.get_spreadsheet_edit_session(session_id)
    assert session["new_version_id"] == version_id
    assert session["operation"] == "edit"
    assert session["change_count"] == 1
    stored = db._conn.execute(
        "SELECT action, new_sheet_name, affected_nonempty_cells "
        "FROM spreadsheet_structure_changes WHERE session_id=?",
        (session_id,),
    ).fetchone()
    assert tuple(stored) == ("add_sheet", "August", 0)
