from openpyxl import Workbook

from rag.spreadsheets.migration import migrate_existing_spreadsheets
from rag.user_db import UserDB


def test_migration_creates_dataset_without_changing_current_document(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    owner = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("sales.xlsx", "rag_docs", owner)
    source = tmp_path / "sales.xlsx"
    book = Workbook()
    book.active.append(["region"])
    book.active.append(["east"])
    book.save(source)
    version_id = db.create_document_version(document_id, "sales.xlsx", str(source), "hash", owner)
    db.activate_document_version(document_id, version_id, "rag_docs", "sales.xlsx")

    report = migrate_existing_spreadsheets(db, tmp_path / "datasets")

    assert report.migrated == 1
    assert db.get_current_document_version(document_id)["id"] == version_id
    assert db.get_structured_dataset_for_version(version_id)["status"] == "ready"
