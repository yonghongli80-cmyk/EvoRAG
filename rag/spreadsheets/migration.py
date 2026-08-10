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
        dataset_id = db.create_structured_dataset(document["id"], version_id, fmt, "row_groups")
        try:
            write_dataset(resolve_dataset_path(root, document["id"], version_id), parse_spreadsheet(Path(version["file_path"]), fmt, None))
            db.activate_structured_dataset(dataset_id)
            migrated += 1
        except Exception as exc:
            db.mark_structured_dataset_failed(dataset_id, type(exc).__name__)
            failed += 1
    return SpreadsheetMigrationReport(migrated, failed, unchanged)
