import sqlite3

import pytest

from rag.user_db import UserDB


def _versioned_spreadsheet_db(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    owner_id = db.create_user("sheet-owner", "Password-123!")
    document_id = db.create_document_permission("sales.xlsx", "rag_docs", owner_id)
    version_id = db.create_document_version(
        document_id, "sales.xlsx", str(tmp_path / "sales.xlsx"), "sheet-hash", owner_id
    )
    db.activate_document_version(document_id, version_id, "rag_docs", "sales.xlsx")
    return db, document_id, version_id


def test_one_structured_dataset_per_document_version(tmp_path):
    db, document_id, version_id = _versioned_spreadsheet_db(tmp_path)

    first = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")

    with pytest.raises(sqlite3.IntegrityError):
        db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    assert db.get_structured_dataset_for_version(version_id)["id"] == first


def test_dataset_activation_is_version_scoped(tmp_path):
    db, document_id, version_id = _versioned_spreadsheet_db(tmp_path)
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "schema_only")

    db.activate_structured_dataset(dataset_id)

    dataset = db.get_structured_dataset_for_version(version_id)
    assert dataset["status"] == "ready"
    assert dataset["document_id"] == document_id
