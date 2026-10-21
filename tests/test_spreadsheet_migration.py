from openpyxl import Workbook

from rag.spreadsheets.migration import (
    ensure_structured_dataset,
    migrate_existing_spreadsheets,
)
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset
from rag.user_db import UserDB


def _write_xlsx(path, header, rows):
    book = Workbook()
    sheet = book.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


def _legacy_dataset(db, root, tmp_path, *, kb_id="kb_sales", filename="sales.xlsx"):
    """Reproduce a pre-fix dataset: the file has a manifest, users.db has none."""
    owner = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission(filename, kb_id, owner)
    source = _write_xlsx(
        tmp_path / filename,
        ["品类", "销售额"],
        [["数码", 12.5], ["家电", 8]],
    )
    version_id = db.create_document_version(document_id, filename, str(source), "hash", owner)
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    write_dataset(
        resolve_dataset_path(root, document_id, version_id),
        parse_spreadsheet(source, "xlsx", None),
    )
    db.activate_document_and_dataset(
        document_id=document_id,
        version_id=version_id,
        dataset_id=dataset_id,
        kb_id=kb_id,
        filename=filename,
    )
    return {
        "owner": owner,
        "document_id": document_id,
        "version_id": version_id,
        "dataset_id": dataset_id,
        "source": source,
    }


def _sheet_payload():
    return (
        {
            "sheet_key": "sheet_0001",
            "name": "销量",
            "ordinal": 1,
            "visibility": "visible",
            "header_row": 1,
            "first_data_row": 2,
            "row_count": 2,
            "column_count": 1,
            "columns": (
                {
                    "column_key": "c_0001",
                    "ordinal": 1,
                    "display_name": "销售额",
                    "normalized_name": "销售额",
                    "inferred_type": "decimal",
                    "number_format": "0.00",
                },
            ),
        },
    )


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


def test_migration_writes_schema_mirror_for_new_dataset(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    owner = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("sales.xlsx", "kb_sales", owner)
    source = _write_xlsx(
        tmp_path / "sales.xlsx", ["品类", "销售额"], [["数码", 12.5], ["家电", 8]]
    )
    version_id = db.create_document_version(document_id, "sales.xlsx", str(source), "hash", owner)
    db.activate_document_version(document_id, version_id, "kb_sales", "sales.xlsx")

    report = migrate_existing_spreadsheets(db, tmp_path / "datasets")

    dataset = db.get_structured_dataset_for_version(version_id)
    schema = db.list_structured_dataset_schema(dataset["id"])
    assert report.migrated == 1
    assert report.mirrored == 0
    assert [row["display_name"] for row in schema] == ["品类", "销售额"]
    assert [row["inferred_type"] for row in schema] == ["text", "decimal"]
    assert schema[0]["sheet_name"] == "Sheet"
    assert schema[0]["row_count"] == 2
    assert schema[0]["column_key"] == "c_0001"
    assert schema[1]["unit"] == ""
    assert schema[1]["sample_values_json"] == "[]"
    db.close()


def test_replace_structured_dataset_schema_is_idempotent(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    owner = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("sales.xlsx", "kb_sales", owner)
    version_id = db.create_document_version(document_id, "sales.xlsx", "sales.xlsx", "hash", owner)
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")

    db.replace_structured_dataset_schema(dataset_id, _sheet_payload())
    first = db.list_structured_dataset_schema(dataset_id)
    db.replace_structured_dataset_schema(dataset_id, _sheet_payload())
    second = db.list_structured_dataset_schema(dataset_id)

    assert first == second
    assert len(second) == 1
    assert second[0]["display_name"] == "销售额"
    assert second[0]["unit"] == ""
    assert second[0]["sample_values_json"] == "[]"
    with db.locked_read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM structured_sheets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM structured_columns").fetchone()[0] == 1
    db.close()


def test_mirror_failure_marks_new_dataset_failed(tmp_path, monkeypatch):
    db = UserDB(str(tmp_path / "users.db"))
    owner = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("sales.xlsx", "kb_sales", owner)
    source = _write_xlsx(tmp_path / "sales.xlsx", ["品类"], [["数码"]])
    version_id = db.create_document_version(document_id, "sales.xlsx", str(source), "hash", owner)
    db.activate_document_version(document_id, version_id, "kb_sales", "sales.xlsx")

    def _fail(*args, **kwargs):
        raise RuntimeError("mirror write failed")

    monkeypatch.setattr(db, "replace_structured_dataset_schema", _fail)

    report = migrate_existing_spreadsheets(db, tmp_path / "datasets")

    dataset = db.get_structured_dataset_for_version(version_id)
    assert report.failed == 1
    assert report.mirrored == 0
    assert dataset["status"] == "failed"
    assert dataset["last_error_code"] == "RuntimeError"
    db.close()


def test_backfill_repairs_legacy_dataset_without_mirror_rows(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    root = tmp_path / "datasets"
    fixture = _legacy_dataset(db, root, tmp_path)
    assert db.list_structured_dataset_schema(fixture["dataset_id"]) == []

    report = migrate_existing_spreadsheets(db, root)

    schema = db.list_structured_dataset_schema(fixture["dataset_id"])
    assert report.mirrored == 1
    assert [row["display_name"] for row in schema] == ["品类", "销售额"]
    assert [row["column_key"] for row in schema] == ["c_0001", "c_0002"]

    again = migrate_existing_spreadsheets(db, root)
    assert again.mirrored == 0
    assert db.list_structured_dataset_schema(fixture["dataset_id"]) == schema
    db.close()


def test_existing_dataset_repairs_missing_mirror_without_recreating(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    root = tmp_path / "datasets"
    fixture = _legacy_dataset(db, root, tmp_path)

    created = ensure_structured_dataset(
        db,
        root,
        document_id=fixture["document_id"],
        version_id=fixture["version_id"],
        source_path=fixture["source"],
        fmt="xlsx",
    )

    assert created is False
    assert [row["display_name"] for row in db.list_structured_dataset_schema(fixture["dataset_id"])] == [
        "品类",
        "销售额",
    ]
    db.close()


def test_backfill_repairs_zero_registry_counters_for_existing_mirror(tmp_path):
    """Mirror rows from the older path must not leave the catalog at zero tables."""
    db = UserDB(str(tmp_path / "users.db"))
    root = tmp_path / "datasets"
    fixture = _legacy_dataset(db, root, tmp_path)
    migrate_existing_spreadsheets(db, root)

    # The exact production state observed on 2026-10-21: mirror rows exist,
    # every registry counter is still zero.
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE structured_datasets SET sheet_count=0,total_rows=0,total_columns=0 WHERE id=?",
            (fixture["dataset_id"],),
        )
    before = db.get_structured_dataset_for_version(fixture["version_id"])
    assert (before["sheet_count"], before["total_rows"], before["total_columns"]) == (0, 0, 0)

    report = migrate_existing_spreadsheets(db, root)

    after = db.get_structured_dataset_for_version(fixture["version_id"])
    assert report.migrated == 0
    assert report.mirrored == 0
    assert (after["sheet_count"], after["total_rows"], after["total_columns"]) == (1, 2, 2)
    db.close()
