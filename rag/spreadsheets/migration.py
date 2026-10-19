"""Best-effort migration of existing XLSX/CSV document versions."""
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import (
    read_dataset_schema,
    resolve_dataset_path,
    write_dataset,
)
from rag.user_db import UserDB

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpreadsheetMigrationReport:
    migrated: int
    failed: int
    unchanged: int
    mirrored: int = 0


def dataset_schema_mirror(dataset_path: Path) -> tuple[dict[str, Any], ...]:
    """Build the ``users.db`` schema mirror payload from one dataset file.

    The dataset file is the source of truth, so this only reads its manifest.
    ``unit`` and ``sample_values_json`` are not recorded by the dataset format;
    they are written as the empty defaults instead of invented values.
    """
    return tuple(
        {
            "sheet_key": sheet.sheet_key,
            "name": sheet.name,
            "ordinal": sheet.ordinal,
            "visibility": sheet.visibility,
            "header_row": sheet.header_row,
            "first_data_row": sheet.first_data_row,
            "row_count": sheet.row_count,
            "column_count": sheet.column_count,
            "merged_range_count": sheet.merged_range_count,
            "has_formulas": sheet.has_formulas,
            "columns": tuple(
                {
                    "column_key": column.column_key,
                    "ordinal": column.ordinal,
                    "display_name": column.display_name,
                    "normalized_name": column.normalized_name,
                    "inferred_type": column.inferred_type,
                    "number_format": column.number_format,
                    "unit": "",
                    "sample_values_json": "[]",
                }
                for column in sheet.columns
            ),
        }
        for sheet in read_dataset_schema(dataset_path)
    )


def _mirror_dataset_schema(
    db: UserDB, datasets_root: Path, dataset: Mapping[str, Any]
) -> bool:
    """Write the mirror rows of one ready dataset whose mirror is still empty.

    Best-effort by contract: the caller decides whether a failure is fatal.
    """
    dataset_id = int(dataset["id"])
    status = dataset.get("status")
    if status is not None and str(status) != "ready":
        return False
    if db.list_structured_dataset_schema(dataset_id):
        return False
    path = (datasets_root / str(dataset["storage_key"])).resolve()
    if path != datasets_root and datasets_root not in path.parents:
        logger.warning("dataset %s storage key escapes the datasets root", dataset_id)
        return False
    if not path.is_file():
        logger.warning("dataset %s file is missing: %s", dataset_id, path)
        return False
    db.replace_structured_dataset_schema(dataset_id, dataset_schema_mirror(path))
    return True


def ensure_structured_dataset(
    db: UserDB,
    root: Path,
    *,
    document_id: int,
    version_id: int,
    source_path: Path,
    fmt: str,
) -> bool:
    """Create the readable dataset for one spreadsheet version if absent.

    Returns ``True`` when a new dataset was created and ``False`` when a ready
    dataset already existed.  The version file remains the source of truth;
    this database is a rebuildable, immutable read model.

    A dataset created by another path (the material worker indexes XLSX
    versions too) may already exist without mirror rows, so the existing branch
    repairs the mirror before reporting it as unchanged.
    """
    existing = db.get_structured_dataset_for_version(version_id)
    if existing is not None:
        try:
            _mirror_dataset_schema(db, Path(root).resolve(), existing)
        except Exception:
            logger.exception(
                "dataset %s schema mirror repair failed", existing.get("id")
            )
        return False
    dataset_id = db.create_structured_dataset(
        document_id, version_id, fmt, "row_groups"
    )
    try:
        dataset_path = resolve_dataset_path(root, document_id, version_id)
        workbook = parse_spreadsheet(Path(source_path), fmt, None)
        write_dataset(dataset_path, workbook)
        db.replace_structured_dataset_schema(
            dataset_id, dataset_schema_mirror(dataset_path)
        )
        db.activate_structured_dataset(dataset_id)
    except Exception as exc:
        db.mark_structured_dataset_failed(dataset_id, type(exc).__name__)
        raise
    return True


def backfill_structured_dataset_mirrors(
    db: UserDB, root: Path, kb_ids: set[str]
) -> int:
    """Copy the manifest of each ready dataset that has no mirror rows yet.

    Datasets created before the schema mirror existed have no
    ``structured_sheets`` / ``structured_columns`` rows, so the catalog sees
    zero tables for them.  Their manifest lives inside the dataset file; this
    repairs it without re-parsing the original workbook and stays idempotent by
    skipping datasets that already have mirror rows.
    """
    datasets_root = Path(root).resolve()
    mirrored = 0
    for kb_id in sorted(kb_ids):
        for item in db.list_current_ready_spreadsheet_datasets(kb_id):
            try:
                if _mirror_dataset_schema(db, datasets_root, item):
                    mirrored += 1
            except Exception:
                logger.exception(
                    "dataset %s schema mirror backfill failed", item.get("id")
                )
    return mirrored


def migrate_existing_spreadsheets(db: UserDB, root: Path) -> SpreadsheetMigrationReport:
    documents = db.list_document_permissions_all()
    migrated = failed = unchanged = 0
    for document in documents:
        version_id = document.get("current_version_id")
        if not version_id or Path(document["doc_name"]).suffix.lower() not in {".xlsx", ".csv"}:
            continue
        if db.get_structured_dataset_for_version(version_id):
            unchanged += 1
            continue
        version = db.get_document_version_by_id(version_id)
        fmt = Path(document["doc_name"]).suffix.lower().removeprefix(".")
        try:
            ensure_structured_dataset(
                db,
                root,
                document_id=document["id"],
                version_id=version_id,
                source_path=Path(version["file_path"]),
                fmt=fmt,
            )
            migrated += 1
        except Exception:
            failed += 1
    mirrored = backfill_structured_dataset_mirrors(
        db, root, {str(document.get("kb_id") or "") for document in documents}
    )
    return SpreadsheetMigrationReport(migrated, failed, unchanged, mirrored)
