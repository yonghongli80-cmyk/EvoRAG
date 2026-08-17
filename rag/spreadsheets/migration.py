"""Best-effort migration of existing XLSX/CSV document versions."""
from dataclasses import dataclass
from pathlib import Path

from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset
from rag.user_db import UserDB


@dataclass(frozen=True)
class SpreadsheetMigrationReport:
    migrated: int
    failed: int
    unchanged: int


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
    """
    existing = db.get_structured_dataset_for_version(version_id)
    if existing is not None:
        return False
    dataset_id = db.create_structured_dataset(
        document_id, version_id, fmt, "row_groups"
    )
    try:
        workbook = parse_spreadsheet(Path(source_path), fmt, None)
        write_dataset(
            resolve_dataset_path(root, document_id, version_id), workbook
        )
        db.activate_structured_dataset(dataset_id)
    except Exception as exc:
        db.mark_structured_dataset_failed(dataset_id, type(exc).__name__)
        raise
    return True


def migrate_existing_spreadsheets(db: UserDB, root: Path) -> SpreadsheetMigrationReport:
    migrated = failed = unchanged = 0
    for document in db.list_document_permissions_all():
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
    return SpreadsheetMigrationReport(migrated, failed, unchanged)
