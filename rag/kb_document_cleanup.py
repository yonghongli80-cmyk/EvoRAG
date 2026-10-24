"""Confined cleanup for KB document files and immutable version files."""

from pathlib import Path

from rag.document_version_store import resolve_version_path
from rag.kb_file_store import resolve_stored_path


def _resolve_owned_path(root: Path, stored_path: str, owner_parts: tuple[str, ...], resolver) -> Path:
    """Resolve a path only when its lexical and resolved owner both match."""
    root_path = root.absolute()
    candidate = Path(stored_path)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    candidate = candidate.absolute()
    owner_root = root_path.joinpath(*owner_parts)
    if not candidate.is_relative_to(owner_root):
        raise ValueError("stored path belongs to another document")

    current = root_path
    for part in candidate.relative_to(root_path).parts:
        current = current / part
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (is_junction is not None and is_junction()):
            raise ValueError("stored path crosses a link")

    resolved = resolver(root, str(candidate))
    if not resolved.is_relative_to(owner_root.resolve()):
        raise ValueError("resolved path belongs to another document")
    return resolved


def resolve_kb_document_paths(
    user_db, kb_id: str, record: dict, versions_root: Path, kb_files_root: Path
) -> list[Path]:
    """Resolve and validate every file owned by a KB document before deletion."""
    candidates: set[Path] = set()
    permission = user_db.get_document_permission(record["filename"], kb_id)
    if permission:
        for version in user_db.list_document_versions(permission["id"]):
            candidates.add(
                _resolve_owned_path(
                    versions_root,
                    version["file_path"],
                    (str(permission["id"]),),
                    resolve_version_path,
                )
            )

    stored_path = record.get("file_path")
    if stored_path:
        path = Path(stored_path)
        if path.is_absolute() and path.absolute().is_relative_to(versions_root.absolute()):
            if permission is None:
                raise ValueError("version path has no owning document")
            candidates.add(
                _resolve_owned_path(
                    versions_root,
                    str(path),
                    (str(permission["id"]),),
                    resolve_version_path,
                )
            )
        else:
            candidates.add(
                _resolve_owned_path(
                    kb_files_root,
                    str(path),
                    (kb_id, str(record["id"])),
                    resolve_stored_path,
                )
            )
    return sorted(candidates)


def delete_kb_document_files(paths: list[Path]) -> None:
    """Remove prevalidated files, leaving database identities for retry on error."""
    for path in paths:
        path.unlink(missing_ok=True)
