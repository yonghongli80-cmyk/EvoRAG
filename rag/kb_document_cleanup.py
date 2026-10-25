"""Confined cleanup for KB document files and immutable version files."""

import hashlib
from pathlib import Path

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


def _version_owner_parts(
    parts: tuple[str, ...], *, document_id: int, kb_id: str, allow_kb_staging: bool
) -> bool:
    """Return whether a relative version path has one of its owner's layouts."""
    document_part = str(document_id)
    if (
        len(parts) >= 3
        and parts[0] == document_part
        and parts[1].isdecimal()
        and int(parts[1]) > 0
    ):
        return True
    if len(parts) >= 3 and parts[:2] == ("staging", document_part):
        return True
    if allow_kb_staging and len(parts) >= 4:
        kb_hash = hashlib.sha256(kb_id.encode("utf-8")).hexdigest()
        return parts[:3] == ("staging", "kb", kb_hash)
    return False


def _resolve_owned_version_path(
    root: Path,
    stored_path: str,
    *,
    document_id: int,
    kb_id: str,
    allow_kb_staging: bool,
) -> Path:
    """Resolve a version only inside its final or explicitly-owned staging namespace."""
    if (
        isinstance(document_id, bool)
        or not isinstance(document_id, int)
        or document_id <= 0
        or not isinstance(kb_id, str)
        or not kb_id
        or not isinstance(stored_path, str)
        or not stored_path
    ):
        raise ValueError("version_path_unowned")

    root_path = Path(root).absolute()
    candidate = Path(stored_path)
    if ".." in candidate.parts:
        raise ValueError("version_path_unowned")
    if not candidate.is_absolute():
        candidate = root_path / candidate
    candidate = candidate.absolute()
    try:
        relative_parts = candidate.relative_to(root_path).parts
    except ValueError as exc:
        raise ValueError("version_path_unowned") from exc
    if not _version_owner_parts(
        relative_parts,
        document_id=document_id,
        kb_id=kb_id,
        allow_kb_staging=allow_kb_staging,
    ):
        raise ValueError("version_path_unowned")

    current = root_path
    for part in relative_parts:
        current = current / part
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (is_junction is not None and is_junction()):
            raise ValueError("version_path_unowned")

    root_resolved = root_path.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError("version_path_unowned")
    try:
        resolved_parts = resolved.relative_to(root_resolved).parts
    except ValueError as exc:
        raise ValueError("version_path_unowned") from exc
    if not _version_owner_parts(
        resolved_parts,
        document_id=document_id,
        kb_id=kb_id,
        allow_kb_staging=allow_kb_staging,
    ):
        raise ValueError("version_path_unowned")
    return resolved


def resolve_owned_version_path(
    root: Path, stored_path: str, *, document_id: int, kb_id: str
) -> Path:
    """Resolve a version file owned by a document or its KB's hashed staging area.

    This helper is deliberately independent of settings and database state so read-only
    diagnostics can apply the same owner proof to explicit roots and snapshots.
    """
    return _resolve_owned_version_path(
        root,
        stored_path,
        document_id=document_id,
        kb_id=kb_id,
        allow_kb_staging=True,
    )


def resolve_kb_document_paths(
    user_db,
    kb_id: str,
    record: dict,
    versions_root: Path,
    kb_files_root: Path,
    *,
    kb_wide: bool = False,
) -> list[Path]:
    """Resolve and validate every file owned by a KB document before deletion."""
    candidates: set[Path] = set()
    permission = user_db.get_document_permission(record["filename"], kb_id)
    if permission:
        for version in user_db.list_document_versions(permission["id"]):
            candidates.add(
                _resolve_owned_version_path(
                    versions_root,
                    version["file_path"],
                    document_id=int(permission["id"]),
                    kb_id=kb_id,
                    allow_kb_staging=kb_wide,
                )
            )

    stored_path = record.get("file_path")
    if stored_path:
        path = Path(stored_path)
        if path.is_absolute() and path.absolute().is_relative_to(versions_root.absolute()):
            if permission is None:
                raise ValueError("version path has no owning document")
            candidates.add(
                _resolve_owned_version_path(
                    versions_root,
                    str(path),
                    document_id=int(permission["id"]),
                    kb_id=kb_id,
                    allow_kb_staging=kb_wide,
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


def resolve_document_staging_directory(root: Path, document_id: int) -> Path:
    """Resolve the explicit document_id-owned staging directory for teardown."""
    if not isinstance(document_id, int) or document_id <= 0:
        raise ValueError("invalid document_id")
    root_path = Path(root).absolute()
    staging_root = root_path / "staging"
    candidate = staging_root / str(document_id)
    current = root_path
    for part in candidate.relative_to(root_path).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("staging directory crosses a link")
    if not candidate.resolve().is_relative_to(staging_root.resolve()):
        raise ValueError("staging directory escapes root")
    return candidate
