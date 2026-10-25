"""Retryable, owner-scoped cleanup for complete knowledge-base deletion."""

from __future__ import annotations

import hashlib
import shutil
import stat
from pathlib import Path

from rag.kb_document_cleanup import (
    _require_regular_file_if_present,
    delete_kb_document_files,
    resolve_document_staging_directory,
    resolve_kb_document_paths,
    resolve_owned_version_path,
)
from rag.kb_lifecycle import KBDeleteIncomplete
from rag.spreadsheets.store import resolve_dataset_path


def _owned_directory(root: Path, owner: str) -> Path:
    root = Path(root).absolute()
    path = root / owner
    if path.is_symlink() or (getattr(path, "is_junction", lambda: False)()):
        raise ValueError("owned directory crosses a link")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("owned directory escapes root")
    return path


def _validate_tree(path: Path, root: Path) -> Path:
    """Reject symlinks/junctions anywhere in a tree before recursive removal."""
    root = Path(root).absolute()
    candidate = Path(path).absolute()
    if not candidate.is_relative_to(root):
        raise ValueError("cleanup path escapes root")
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("cleanup path crosses a link")
    if candidate.exists():
        for child in candidate.rglob("*"):
            if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                raise ValueError("cleanup tree contains a link")
    return candidate


def _require_directory_if_present(path: Path) -> None:
    """Accept a missing cleanup root but reject existing non-directory paths."""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError("cleanup_path_invalid") from exc
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError("cleanup_path_invalid")


def _path_references_cleanup_target(
    stored_path: str,
    *,
    default_root: Path,
    planned_paths: set[Path],
    planned_dirs: set[Path],
) -> bool:
    """Compare stored references to planned files and recursively removed directories."""
    if not stored_path:
        return False
    candidate = Path(stored_path)
    if not candidate.is_absolute():
        candidate = Path(default_root) / candidate
    try:
        candidates = {candidate.absolute(), candidate.resolve()}
    except (OSError, RuntimeError):
        candidates = {candidate.absolute()}
    for reference in candidates:
        if reference in planned_paths:
            return True
        if any(reference.is_relative_to(directory) for directory in planned_dirs):
            return True
    return False


def collect_kb_cleanup(db, kb_id: str, versions_root: Path, kb_files_root: Path, datasets_root: Path):
    """Snapshot database-owned identities and prevalidate all filesystem paths."""
    all_permissions = db.list_document_permissions_all()
    permissions = [row for row in all_permissions if row["kb_id"] == kb_id]
    all_permission_by_id = {int(row["id"]): row for row in all_permissions}
    permission_by_id = {int(row["id"]): row for row in permissions}
    documents = db.list_kb_documents(kb_id)
    versions_by_document_id = {}

    def load_document_versions(document_id: int):
        if document_id not in versions_by_document_id:
            versions_by_document_id[document_id] = db.list_document_versions(document_id)
        return versions_by_document_id[document_id]

    document_ids = set(permission_by_id)
    paths: set[Path] = set()
    for row in documents:
        document_ids.add(int(row["id"]))

    datasets = []
    all_versions = []
    with db.cm.read_connection() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='structured_datasets'").fetchone():
            datasets = conn.execute(
                "SELECT id, document_id, document_version_id FROM structured_datasets "
                "WHERE document_id IN (" + (",".join("?" for _ in permission_by_id) or "NULL") + ")",
                tuple(permission_by_id),
            ).fetchall()
        all_versions = conn.execute(
            "SELECT id, document_id, file_path FROM document_versions"
        ).fetchall()
        archive_manifests = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_archive_manifests'").fetchone():
            archive_manifests = conn.execute(
                "SELECT id, container_document_id, container_version_id, source_path "
                "FROM material_archive_manifests"
            ).fetchall()

    kb_hash = hashlib.sha256(kb_id.encode("utf-8")).hexdigest()
    versions_by_id = {int(row["id"]): row for row in all_versions}
    target_document_ids = set(permission_by_id)
    target_manifest_ids = []
    target_manifests = []
    for manifest in archive_manifests:
        container_document_id = int(manifest["container_document_id"])
        permission = permission_by_id.get(container_document_id)
        if permission is None or permission["kb_id"] != kb_id:
            continue
        version = versions_by_id.get(int(manifest["container_version_id"]))
        if version is None or int(version["document_id"]) != container_document_id:
            raise KBDeleteIncomplete("archive_identity_mismatch")
        target_manifest_ids.append(int(manifest["id"]))
        target_manifests.append(manifest)

    # A manifest source must remain in its KB-owned quarantine; the version itself
    # can separately be at the container document's final path after activation.
    for manifest in target_manifests:
        source_path = str(manifest["source_path"] or "").strip()
        if not source_path:
            continue
        source_candidate = Path(source_path)
        if not source_candidate.is_absolute():
            source_candidate = Path(versions_root) / source_candidate
        source_candidate = source_candidate.absolute()
        if not source_candidate.exists() and not source_candidate.is_symlink() and not getattr(source_candidate, "is_junction", lambda: False)():
            continue
        container_document_id = int(manifest["container_document_id"])
        try:
            source = resolve_owned_version_path(
                versions_root,
                str(source_candidate),
                document_id=container_document_id,
                kb_id=kb_id,
            )
        except ValueError as exc:
            raise KBDeleteIncomplete("archive_source_unowned") from exc
        source_parts = source.relative_to(Path(versions_root).resolve()).parts
        in_kb_staging = len(source_parts) >= 4 and source_parts[:3] == (
            "staging",
            "kb",
            kb_hash,
        )
        if not in_kb_staging:
            raise KBDeleteIncomplete("archive_source_unowned")
        try:
            paths.add(_validate_tree(source, Path(versions_root)))
        except ValueError as exc:
            raise KBDeleteIncomplete("archive_source_unowned") from exc

    # Validate archive identity and source ownership before version paths so a
    # legacy global archive reports archive_source_unowned deterministically.
    for row in documents:
        try:
            paths.update(
                resolve_kb_document_paths(
                    db,
                    kb_id,
                    row,
                    versions_root,
                    kb_files_root,
                    kb_wide=True,
                    version_loader=load_document_versions,
                )
            )
        except ValueError as exc:
            if str(exc) in {"version_path_unowned", "cleanup_path_invalid"}:
                raise KBDeleteIncomplete(str(exc)) from exc
            raise

    # Include version files for permissions that do not have a directory row.
    for permission_id in permission_by_id:
        for version in load_document_versions(permission_id):
            try:
                paths.add(
                    resolve_owned_version_path(
                        versions_root,
                        version["file_path"],
                        document_id=permission_id,
                        kb_id=kb_id,
                    )
                )
            except ValueError as exc:
                raise KBDeleteIncomplete("version_path_unowned") from exc

    for dataset in datasets:
        expected = resolve_dataset_path(
            datasets_root, int(dataset["document_id"]), int(dataset["document_version_id"])
        )
        validated = _validate_tree(expected, datasets_root)
        try:
            _require_regular_file_if_present(validated, "cleanup_path_invalid")
        except ValueError as exc:
            raise KBDeleteIncomplete("cleanup_path_invalid") from exc
        paths.add(validated)

    material_staging = _owned_directory(Path(versions_root) / "staging" / "kb", kb_hash)
    # Task 3 document_id-scoped staging; only remove direct positive numeric owners.
    document_staging = [
        resolve_document_staging_directory(versions_root, document_id)
        for document_id in permission_by_id
    ]

    cleanup_dirs = []
    for path in [material_staging, *document_staging]:
        validated = _validate_tree(path, Path(versions_root))
        try:
            _require_directory_if_present(validated)
        except ValueError as exc:
            raise KBDeleteIncomplete("cleanup_path_invalid") from exc
        cleanup_dirs.append(validated)
    # Deduplicate nested staging roots so their parent is removed once.
    cleanup_dirs = sorted(set(cleanup_dirs), key=lambda path: len(path.parts))
    cleanup_dirs = [
        path for path in cleanup_dirs
        if not any(parent != path and path.is_relative_to(parent) for parent in cleanup_dirs)
    ]

    planned_paths = set(paths)
    planned_paths.update(path.resolve() for path in paths)
    planned_dirs = {path.absolute() for path in cleanup_dirs}
    planned_dirs.update(path.resolve() for path in cleanup_dirs)
    target_version_ids = {
        int(version["id"])
        for version in all_versions
        if int(version["document_id"]) in permission_by_id
    }
    for version in all_versions:
        document_id = int(version["document_id"])
        permission = all_permission_by_id.get(document_id)
        if permission is not None and permission["kb_id"] == kb_id:
            continue
        if _path_references_cleanup_target(
            str(version["file_path"] or ""),
            default_root=Path(versions_root),
            planned_paths=planned_paths,
            planned_dirs=planned_dirs,
        ):
            raise KBDeleteIncomplete("cleanup_path_shared")

    for manifest in archive_manifests:
        container_document_id = int(manifest["container_document_id"])
        is_target_manifest = int(manifest["id"]) in target_manifest_ids
        permission = all_permission_by_id.get(container_document_id)
        is_target_owner = permission is not None and permission["kb_id"] == kb_id
        if is_target_manifest:
            continue
        if is_target_owner:
            raise KBDeleteIncomplete("archive_identity_mismatch")
        references_target_identity = (
            container_document_id in target_document_ids
            or int(manifest["container_version_id"]) in target_version_ids
        )
        references_target_path = _path_references_cleanup_target(
            str(manifest["source_path"] or ""),
            default_root=Path(versions_root),
            planned_paths=planned_paths,
            planned_dirs=planned_dirs,
        )
        if references_target_identity or references_target_path:
            raise KBDeleteIncomplete("cleanup_path_shared")

    return {
        "permissions": permissions,
        "documents": documents,
        "document_ids": sorted(document_ids),
        "paths": sorted(paths),
        "cleanup_dirs": cleanup_dirs,
        "dataset_ids": [int(row["id"]) for row in datasets],
        "archive_manifest_ids": target_manifest_ids,
    }


def cleanup_kb_files(plan: dict) -> None:
    """Remove prevalidated files and owner directories, preserving unrelated staging."""
    delete_kb_document_files(plan["paths"])
    for directory in plan["cleanup_dirs"]:
        if directory.exists():
            shutil.rmtree(directory)


def finish_kb_cleanup_sql(conn, kb_id: str, plan: dict) -> None:
    """Delete KB-owned derived state in the lifecycle terminal transaction."""
    permission_ids = [int(row["id"]) for row in plan["permissions"]]
    if permission_ids:
        marks = ",".join("?" for _ in permission_ids)
        conn.execute(f"DELETE FROM material_search_fts WHERE document_id IN ({marks})", permission_ids)
        conn.execute(f"DELETE FROM material_unit_fingerprints WHERE document_id IN ({marks})", permission_ids)
        conn.execute(f"DELETE FROM document_permissions WHERE id IN ({marks})", permission_ids)
    if plan["dataset_ids"]:
        marks = ",".join("?" for _ in plan["dataset_ids"])
        conn.execute(f"DELETE FROM structured_datasets WHERE id IN ({marks})", plan["dataset_ids"])
    if plan["archive_manifest_ids"]:
        marks = ",".join("?" for _ in plan["archive_manifest_ids"])
        conn.execute(f"DELETE FROM material_archive_entries WHERE manifest_id IN ({marks})", plan["archive_manifest_ids"])
        conn.execute(f"DELETE FROM material_archive_import_results WHERE manifest_id IN ({marks})", plan["archive_manifest_ids"])
        conn.execute(f"DELETE FROM material_archive_manifests WHERE id IN ({marks})", plan["archive_manifest_ids"])
    conn.execute("DELETE FROM kb_documents WHERE kb_id = ?", (kb_id,))
    conn.execute("DELETE FROM kb_shares WHERE kb_id = ?", (kb_id,))
    conn.execute("DELETE FROM kb_org_policies WHERE kb_id = ?", (kb_id,))
    conn.execute("DELETE FROM kb_metadata WHERE kb_id = ?", (kb_id,))
