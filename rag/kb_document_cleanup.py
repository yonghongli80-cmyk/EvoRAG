"""Confined cleanup for KB document files and immutable version files."""

from pathlib import Path

from rag.document_version_store import resolve_version_path
from rag.kb_file_store import resolve_stored_path


def resolve_kb_document_paths(
    user_db, kb_id: str, record: dict, versions_root: Path, kb_files_root: Path
) -> list[Path]:
    """Resolve and validate every file owned by a KB document before deletion."""
    candidates: set[Path] = set()
    permission = user_db.get_document_permission(record["filename"], kb_id)
    if permission:
        for version in user_db.list_document_versions(permission["id"]):
            candidates.add(resolve_version_path(versions_root, version["file_path"]))

    stored_path = record.get("file_path")
    if stored_path:
        path = Path(stored_path)
        if path.is_absolute():
            if path.absolute().is_relative_to(versions_root.absolute()):
                candidates.add(resolve_version_path(versions_root, str(path)))
            else:
                candidates.add(resolve_stored_path(kb_files_root, str(path)))
        else:
            candidates.add(resolve_stored_path(kb_files_root, str(path)))
    return sorted(candidates)


def delete_kb_document_files(paths: list[Path]) -> None:
    """Remove prevalidated files, leaving database identities for retry on error."""
    for path in paths:
        path.unlink(missing_ok=True)
