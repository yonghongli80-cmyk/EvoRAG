from rag.spreadsheets.catalog import build_authorized_catalog
from rag.user_db import UserDB


def test_lists_only_current_ready_spreadsheet_datasets(tmp_path):
    db = UserDB(tmp_path / "users.db")
    owner = db.create_user("owner", "hash")
    document_id = db.create_document_permission("sales.xlsx", "kb_sales", owner, 1, False)
    old_version = db.create_document_version(document_id, "sales.xlsx", "old.xlsx", "old", owner)
    old_dataset = db.create_structured_dataset(document_id, old_version, "xlsx", "row_groups")
    db.activate_document_and_dataset(
        document_id=document_id, version_id=old_version, dataset_id=old_dataset,
        kb_id="kb_sales", filename="sales.xlsx",
    )
    new_version = db.create_document_version(document_id, "sales.xlsx", "new.xlsx", "new", owner)
    new_dataset = db.create_structured_dataset(document_id, new_version, "xlsx", "row_groups")
    db.activate_document_and_dataset(
        document_id=document_id, version_id=new_version, dataset_id=new_dataset,
        kb_id="kb_sales", filename="sales.xlsx",
    )

    rows = db.list_current_ready_spreadsheet_datasets("kb_sales")

    assert [(row["id"], row["document_version_id"]) for row in rows] == [(new_dataset, new_version)]
    assert rows[0]["doc_name"] == "sales.xlsx"
    db.close()


def test_catalog_exposes_authorized_sheet_columns(tmp_path):
    db = UserDB(tmp_path / "users.db")
    owner = db.create_user("owner", "hash")
    document_id = db.create_document_permission("sales.xlsx", "kb_sales", owner, 1, False)
    version_id = db.create_document_version(document_id, "sales.xlsx", "source.xlsx", "hash", owner)
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    with db._lock, db.transaction(immediate=True):
        db._conn.execute("INSERT INTO structured_sheets (dataset_id,sheet_key,name,ordinal,visibility,row_count,column_count) VALUES (?,?,?,?,?,?,?)", (dataset_id, "sheet_0001", "销量", 1, "visible", 2, 1))
        sheet_id = db._conn.execute("SELECT id FROM structured_sheets WHERE dataset_id=?", (dataset_id,)).fetchone()[0]
        db._conn.execute("INSERT INTO structured_columns (sheet_id,column_key,ordinal,display_name,normalized_name,inferred_type,number_format) VALUES (?,?,?,?,?,?,?)", (sheet_id, "c_0001", 1, "销售额", "销售额", "decimal", "0.00"))
    db.activate_document_and_dataset(document_id=document_id, version_id=version_id, dataset_id=dataset_id, kb_id="kb_sales", filename="sales.xlsx")

    catalog = build_authorized_catalog(db, tmp_path, "kb_sales", db.get_user_by_id(owner))

    assert catalog.datasets[0].sheets[0].columns[0].display_name == "销售额"
    db.close()
