"""Retryable, owner-scoped cleanup for complete knowledge-base deletion."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from rag.document_version_store import resolve_version_path
from rag.kb_document_cleanup import (
    delete_kb_document_files,
    resolve_document_staging_directory,
    resolve_kb_document_paths,
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


def collect_kb_cleanup(db, kb_id: str, versions_root: Path, kb_files_root: Path, datasets_root: Path):
    """Snapshot database-owned identities and prevalidate all filesystem paths."""
    permissions = [
        row for row in db.list_document_permissions_all() if row["kb_id"] == kb_id
    ]
    permission_by_id = {int(row["id"]): row for row in permissions}
    documents = db.list_kb_documents(kb_id)
    document_ids = set(permission_by_id)
    paths: set[Path] = set()

    for row in documents:
        paths.update(resolve_kb_document_paths(db, kb_id, row, versions_root, kb_files_root))
        document_ids.add(int(row["id"]))

    # Include version files for permissions that do not have a directory row.
    for permission_id in permission_by_id:
        for version in db.list_document_versions(permission_id):
            paths.add(resolve_version_path(versions_root, version["file_path"]))

    datasets = []
    with db.cm.read_connection() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='structured_datasets'").fetchone():
            datasets = conn.execute(
                "SELECT id, document_id, document_version_id FROM structured_datasets "
                "WHERE document_id IN (" + (",".join("?" for _ in permission_by_id) or "NULL") + ")",
                tuple(permission_by_id),
            ).fetchall()
        archive_manifests = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_archive_manifests'").fetchone():
            archive_manifests = conn.execute(
                "SELECT id, container_document_id, source_path FROM material_archive_manifests "
                "WHERE container_document_id IN (" + (",".join("?" for _ in permission_by_id) or "NULL") + ")",
                tuple(permission_by_id),
            ).fetchall()

    for dataset in datasets:
        expected = resolve_dataset_path(
            datasets_root, int(dataset["document_id"]), int(dataset["document_version_id"])
        )
        paths.add(_validate_tree(expected, datasets_root))

    kb_hash = hashlib.sha256(kb_id.encode("utf-8")).hexdigest()
    material_staging = _owned_directory(Path(versions_root) / "staging" / "kb", kb_hash)
    # Task 3 document_id-scoped staging; only remove direct positive numeric owners.
    document_staging = [
        resolve_document_staging_directory(versions_root, document_id)
        for document_id in permission_by_id
    ]
    # Archive manifests may point to legacy global staging. Only hashed KB-owned roots
    # have a proven KB owner; retain the manifest and fail before teardown otherwise.
    for manifest in archive_manifests:
        source_path = str(manifest["source_path"] or "").strip()
        if not source_path:
            continue
        source = Path(source_path).absolute()
        if not source.exists():
            continue
        if not source.is_relative_to(material_staging.absolute()):
            raise KBDeleteIncomplete("archive_source_unowned")
        paths.add(_validate_tree(source, Path(versions_root)))

    cleanup_dirs = [
        _validate_tree(path, Path(versions_root))
        for path in [material_staging, *document_staging]
    ]
    # Deduplicate nested staging roots so their parent is removed once.
    cleanup_dirs = sorted(set(cleanup_dirs), key=lambda path: len(path.parts))
    cleanup_dirs = [
        path for path in cleanup_dirs
        if not any(parent != path and path.is_relative_to(parent) for parent in cleanup_dirs)
    ]
    return {
        "permissions": permissions,
        "documents": documents,
        "document_ids": sorted(document_ids),
        "paths": sorted(paths),
        "cleanup_dirs": cleanup_dirs,
        "dataset_ids": [int(row["id"]) for row in datasets],
        "archive_manifest_ids": [int(row["id"]) for row in archive_manifests],
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
