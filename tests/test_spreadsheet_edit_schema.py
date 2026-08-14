from rag.user_db import UserDB


def _table_names(db: UserDB) -> set[str]:
    return {
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _column_names(db: UserDB, table: str) -> set[str]:
    return {row[1] for row in db._conn.execute(f"PRAGMA table_info({table})")}


def test_spreadsheet_edit_schema_is_idempotent(tmp_path):
    path = tmp_path / "users.db"
    first = UserDB(str(path))
    first.close()

    reopened = UserDB(str(path))
    try:
        assert {
            "spreadsheet_edit_sessions",
            "spreadsheet_cell_changes",
            "spreadsheet_structure_changes",
        } <= _table_names(reopened)
        assert "restored_from_version_id" in _column_names(
            reopened, "document_versions"
        )
    finally:
        reopened.close()


def test_material_schema_rebuild_preserves_restore_provenance_column(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    try:
        db.create_material_schema()

        assert "restored_from_version_id" in _column_names(
            db, "document_versions"
        )
    finally:
        db.close()
