from pathlib import Path

import pytest
from openpyxl import Workbook

from rag.spreadsheets.extractor import ExtractionContext, extract_spreadsheet_version
from rag.spreadsheets.parser import parse_spreadsheet
from rag.user_db import UserDB


def _context(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    owner_id = db.create_user("sheet-owner", "Password-123!")
    document_id = db.create_document_permission("sales.xlsx", "rag_docs", owner_id)
    old_version = db.create_document_version(document_id, "sales.xlsx", "old.xlsx", "old", owner_id)
    old_dataset = db.create_structured_dataset(document_id, old_version, "xlsx", "row_groups")
    db.activate_document_and_dataset(
        document_id=document_id,
        version_id=old_version,
        dataset_id=old_dataset,
        kb_id="rag_docs",
        filename="sales.xlsx",
    )
    version_id = db.create_document_version(document_id, "sales.xlsx", "new.xlsx", "new", owner_id)
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    source = tmp_path / "sales.xlsx"
    book = Workbook()
    book.active.append(["region", "amount"])
    book.active.append(["east", 0])
    book.save(source)
    return db, document_id, old_version, old_dataset, version_id, dataset_id, parse_spreadsheet(source, "xlsx", None)


def test_vector_failure_keeps_old_spreadsheet_current_and_removes_dataset(tmp_path):
    db, document_id, old_version, old_dataset, version_id, dataset_id, workbook = _context(tmp_path)
    dataset_path = tmp_path / "datasets" / "dataset.sqlite3"
    context = ExtractionContext(
        db=db,
        document_id=document_id,
        document_version_id=version_id,
        dataset_id=dataset_id,
        kb_id="rag_docs",
        filename="sales.xlsx",
        dataset_path=dataset_path,
        workbook=workbook,
        write_vectors=lambda _: (_ for _ in ()).throw(RuntimeError("qdrant down")),
        delete_version_vectors=lambda _: None,
        mark_version_current=lambda _: None,
        mark_other_versions_historical=lambda *_: None,
    )

    with pytest.raises(RuntimeError, match="qdrant down"):
        extract_spreadsheet_version(context)

    assert db.get_current_document_version(document_id)["id"] == old_version
    assert db.get_structured_dataset_for_version(old_version)["id"] == old_dataset
    assert db.get_structured_dataset_for_version(version_id)["status"] == "failed"
    assert not dataset_path.exists()


def test_compound_activation_promotes_document_and_dataset_together(tmp_path):
    db, document_id, _, _, version_id, dataset_id, workbook = _context(tmp_path)
    context = ExtractionContext(
        db=db,
        document_id=document_id,
        document_version_id=version_id,
        dataset_id=dataset_id,
        kb_id="rag_docs",
        filename="sales.xlsx",
        dataset_path=tmp_path / "datasets" / "dataset.sqlite3",
        workbook=workbook,
        write_vectors=lambda _: None,
        delete_version_vectors=lambda _: None,
        mark_version_current=lambda _: None,
        mark_other_versions_historical=lambda *_: None,
    )

    extract_spreadsheet_version(context)

    assert db.get_current_document_version(document_id)["id"] == version_id
    assert db.get_structured_dataset_for_version(version_id)["id"] == dataset_id
    assert db.get_structured_dataset_for_version(version_id)["status"] == "ready"


def test_extraction_publishes_the_dataset_with_its_schema_mirror(tmp_path):
    """The catalog reads the users.db mirror, so extraction must write both."""
    db, document_id, _, _, version_id, dataset_id, workbook = _context(tmp_path)
    context = ExtractionContext(
        db=db,
        document_id=document_id,
        document_version_id=version_id,
        dataset_id=dataset_id,
        kb_id="rag_docs",
        filename="sales.xlsx",
        dataset_path=tmp_path / "datasets" / "dataset.sqlite3",
        workbook=workbook,
        write_vectors=lambda _: None,
        delete_version_vectors=lambda _: None,
        mark_version_current=lambda _: None,
        mark_other_versions_historical=lambda *_: None,
    )

    extract_spreadsheet_version(context)

    rows = db.list_structured_dataset_schema(dataset_id)
    assert {row["sheet_key"] for row in rows} == {"sheet_0001"}
    assert {row["column_key"] for row in rows} == {"c_0001", "c_0002"}
    assert {row["display_name"] for row in rows} == {"region", "amount"}


def test_vector_cleanup_failure_enqueues_repair_after_activation(tmp_path):
    db, document_id, _, _, version_id, dataset_id, workbook = _context(tmp_path)
    context = ExtractionContext(
        db=db,
        document_id=document_id,
        document_version_id=version_id,
        dataset_id=dataset_id,
        kb_id="rag_docs",
        filename="sales.xlsx",
        dataset_path=Path(tmp_path / "datasets" / "dataset.sqlite3"),
        workbook=workbook,
        write_vectors=lambda _: None,
        delete_version_vectors=lambda _: None,
        mark_version_current=lambda _: None,
        mark_other_versions_historical=lambda *_: (_ for _ in ()).throw(RuntimeError("cleanup down")),
    )

    extract_spreadsheet_version(context)

    assert db.get_current_document_version(document_id)["id"] == version_id
    assert db.list_vector_flag_repairs() == [{"document_id": document_id, "version_id": version_id}]
