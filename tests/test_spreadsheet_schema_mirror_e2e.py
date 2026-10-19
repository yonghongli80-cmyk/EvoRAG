"""End-to-end coverage for the users.db schema mirror of spreadsheet datasets.

The dataset file is the source of truth.  ``users.db`` only mirrors it, so a
missing mirror makes the catalog report zero tables for a dataset and the
planner can never match a field.  These tests drive the real pipeline:
dataset file -> mirror backfill -> authorized catalog -> planner -> coordinator.
"""
from pathlib import Path

import pytest
from openpyxl import Workbook

from rag.spreadsheets.catalog import (
    CatalogColumn,
    CatalogDataset,
    CatalogSheet,
    SpreadsheetCatalog,
    build_authorized_catalog,
)
from rag.spreadsheets.coordinator import ComputedFact, SpreadsheetQueryCoordinator
from rag.spreadsheets.migration import dataset_schema_mirror, migrate_existing_spreadsheets
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.planner import plan_spreadsheet_query
from rag.spreadsheets.store import read_dataset_schema, resolve_dataset_path, write_dataset
from rag.user_db import UserDB

KB_ID = "kb_accessories"
LENS_HEADER = ["排序", "品牌", "型号", "滤镜尺寸", "遮光罩型号", "遮光罩售价", "前盖", "后盖"]
LENS_ROWS = [
    [1, "腾龙", "17-28mm f/2.8 E口", "67mm", "HA046", 320, "有", "有"],
    [2, "腾龙", "17-70mm f/2.8 E口", "67mm", "HA036", 260, "有", "有"],
    [3, "适马", "18-50mm f/2.8 E口", "55mm", "LH582-01", 180, "有", "有"],
]
PRICE_QUESTION = "遮光罩售价最高的是哪个型号？"
REAL_DATASET_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "structured_datasets"
    / "12"
    / "13"
    / "dataset.sqlite3"
)


def _write_lens_xlsx(path):
    book = Workbook()
    sheet = book.active
    sheet.title = "镜头"
    sheet.append(LENS_HEADER)
    for row in LENS_ROWS:
        sheet.append(row)
    book.save(path)
    return path


def _legacy_lens_dataset(db, root, tmp_path):
    """A ready dataset whose file manifest exists and whose mirror rows do not."""
    owner = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("lens-hood-price.xlsx", KB_ID, owner)
    source = _write_lens_xlsx(tmp_path / "lens-hood-price.xlsx")
    version_id = db.create_document_version(
        document_id, "lens-hood-price.xlsx", str(source), "hash", owner
    )
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    write_dataset(
        resolve_dataset_path(root, document_id, version_id),
        parse_spreadsheet(source, "xlsx", None),
    )
    db.activate_document_and_dataset(
        document_id=document_id,
        version_id=version_id,
        dataset_id=dataset_id,
        kb_id=KB_ID,
        filename="lens-hood-price.xlsx",
    )
    assert db.list_structured_dataset_schema(dataset_id) == []
    return db.get_user_by_id(owner), dataset_id


def _real_dataset_catalog():
    sheets = tuple(
        CatalogSheet(
            sheet.sheet_key,
            sheet.name,
            sheet.row_count,
            tuple(
                CatalogColumn(
                    column.column_key,
                    column.display_name,
                    column.normalized_name,
                    column.inferred_type,
                )
                for column in sheet.columns
            ),
        )
        for sheet in read_dataset_schema(REAL_DATASET_PATH)
    )
    dataset = CatalogDataset(6, 12, 13, REAL_DATASET_PATH.name, REAL_DATASET_PATH, sheets)
    return SpreadsheetCatalog(KB_ID, (dataset,))


def test_question_is_unsupported_until_mirror_is_backfilled_then_planned(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    root = tmp_path / "datasets"
    user, dataset_id = _legacy_lens_dataset(db, root, tmp_path)

    empty_catalog = build_authorized_catalog(db, root, KB_ID, user)
    assert len(empty_catalog.datasets) == 1
    assert empty_catalog.datasets[0].sheets == ()
    assert plan_spreadsheet_query(PRICE_QUESTION, empty_catalog).kind == "unsupported"

    report = migrate_existing_spreadsheets(db, root)

    catalog = build_authorized_catalog(db, root, KB_ID, user)
    assert report.mirrored == 1
    assert len(catalog.datasets[0].sheets) == 1
    assert len(catalog.datasets[0].sheets[0].columns) == len(LENS_HEADER)
    planning = plan_spreadsheet_query(PRICE_QUESTION, catalog)
    assert planning.kind == "planned"
    assert planning.plan is not None
    assert planning.plan.operation == "aggregate"
    assert planning.plan.dataset_ids == (dataset_id,)
    assert planning.plan.metrics[0].column_key == "c_0006"
    assert planning.plan.metrics[0].function == "max"

    outcome = SpreadsheetQueryCoordinator(db, root).query(
        PRICE_QUESTION, kb_id=KB_ID, user=user
    )

    assert outcome.kind == "structured_result"
    assert outcome.rows_scanned == len(LENS_ROWS)
    assert outcome.computed_facts == (ComputedFact("c_0006 max", "320"),)
    db.close()


@pytest.mark.skipif(
    not REAL_DATASET_PATH.is_file(), reason="real redacted dataset fixture is absent"
)
def test_real_dataset_question_is_no_longer_unsupported():
    # The named columns are visible to the planner now; whether the question
    # resolves to one field or needs a clarification is the planner's decision.
    result = plan_spreadsheet_query(PRICE_QUESTION, _real_dataset_catalog())

    assert result.kind != "unsupported"
    assert result.kind in {"planned", "clarification_required"}


@pytest.mark.skipif(
    not REAL_DATASET_PATH.is_file(), reason="real redacted dataset fixture is absent"
)
def test_real_dataset_named_column_yields_planned_aggregate():
    result = plan_spreadsheet_query("遮光罩售价最高是多少？", _real_dataset_catalog())

    assert result.kind == "planned"
    assert result.plan is not None
    assert result.plan.metrics[0].function == "max"
    assert result.plan.metrics[0].column_key == "c_0006"


def test_mirror_rewrites_registry_counters_idempotently(tmp_path):
    """The mirror is the only writer for backfilled datasets, so it owns the counters."""
    db = UserDB(str(tmp_path / "users.db"))
    root = tmp_path / "datasets"
    owner_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("lens-hood-price.xlsx", KB_ID, owner_id)
    source = _write_lens_xlsx(tmp_path / "lens-hood-price.xlsx")
    version_id = db.create_document_version(
        document_id, "lens-hood-price.xlsx", str(source), "hash", owner_id
    )
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    path = resolve_dataset_path(root, document_id, version_id)
    write_dataset(path, parse_spreadsheet(source, "xlsx", None))
    mirror = dataset_schema_mirror(path)

    db.replace_structured_dataset_schema(dataset_id, mirror)
    first = db.get_structured_dataset_for_version(version_id)
    db.replace_structured_dataset_schema(dataset_id, mirror)
    second = db.get_structured_dataset_for_version(version_id)

    expected = (1, len(LENS_ROWS), len(LENS_HEADER))
    assert (first["sheet_count"], first["total_rows"], first["total_columns"]) == expected
    assert (second["sheet_count"], second["total_rows"], second["total_columns"]) == expected
    assert len(db.list_structured_dataset_schema(dataset_id)) == len(LENS_HEADER)
    db.close()
