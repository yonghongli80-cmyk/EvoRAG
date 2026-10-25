"""Read-only audit of an explicit SQLite material snapshot and versions tree.

This module deliberately imports no application settings or database wrappers.
It uses SQLite's immutable read-only URI and only examines filesystem metadata;
it never opens material file contents.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

from rag.kb_document_cleanup import resolve_owned_version_path

SCHEMA_VERSION = 1
_CORE_COLUMNS = {
    "document_permissions": {"id", "doc_name", "kb_id"},
    "document_versions": {
        "id",
        "document_id",
        "version_no",
        "original_filename",
        "file_path",
        "status",
    },
}
_ACTIVE_JOB_STATUSES = {"queued", "retry", "running"}
_PENDING_MANIFEST_STATUSES = {"pending", "pending_confirmation"}


def _empty_report() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "inspect_only",
        "entries": [],
        "findings": [],
        "counts": {"entries": 0, "findings": 0, "by_kind": {}, "by_code": {}},
    }


def _error_report(code: str, message: str) -> dict:
    report = _empty_report()
    report["error"] = {"code": code, "message": message}
    return report


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_link_or_junction(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    if stat.S_ISLNK(mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction())
    except OSError:
        return True


def _has_link_component(path: Path) -> bool:
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_link_or_junction(current):
            return True
    return False


def _lexically_within(path: str | Path, root: str | Path) -> bool:
    try:
        _absolute(path).relative_to(_absolute(root))
    except ValueError:
        return False
    return True


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _root_path(root: Path) -> Path:
    return _absolute(root)


def _path_state(root: Path, path: str | Path, *, want_directory: bool = False) -> str:
    """Inspect each component with lstat, stopping before any link is followed."""
    root_path = _root_path(root)
    candidate = _absolute(path)
    try:
        parts = candidate.relative_to(root_path).parts
    except ValueError:
        return "outside_root_uninspected"

    current = root_path
    if not parts:
        try:
            root_stat = current.lstat()
        except OSError:
            return "missing"
        if _is_link_or_junction(current):
            return "unsafe_link"
        return "present_directory" if stat.S_ISDIR(root_stat.st_mode) else "not_directory"

    for index, part in enumerate(parts):
        current = current / part
        try:
            entry_stat = current.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return "missing"
        except OSError:
            return "unreadable_metadata"
        if _is_link_or_junction(current):
            return "unsafe_link"
        final = index == len(parts) - 1
        if not final and not stat.S_ISDIR(entry_stat.st_mode):
            return "not_directory"
        if final:
            if want_directory:
                return "present_directory" if stat.S_ISDIR(entry_stat.st_mode) else "not_directory"
            return "present" if stat.S_ISREG(entry_stat.st_mode) else "not_regular_file"
    return "missing"


def _sqlite_uri(path: Path) -> str:
    normalized = os.fspath(path).replace("\\", "/")
    return f"file:{quote(normalized, safe='/:')}?mode=ro&immutable=1"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _schema_error(conn: sqlite3.Connection) -> str | None:
    tables = _table_names(conn)
    for table, required_columns in _CORE_COLUMNS.items():
        if table not in tables:
            return f"required table {table} is missing"
        missing = required_columns - _table_columns(conn, table)
        if missing:
            return f"required columns missing from {table}: {', '.join(sorted(missing))}"
    return None


def _select_rows(conn: sqlite3.Connection, table: str, columns: set[str]) -> list[dict]:
    if not columns:
        return []
    selected = ", ".join(f'"{column}"' for column in sorted(columns))
    order = ' ORDER BY "id"' if "id" in columns else ""
    return [dict(row) for row in conn.execute(f'SELECT {selected} FROM "{table}"{order}')]


def _add_finding(
    report: dict,
    code: str,
    *,
    severity: str = "warning",
    subject: dict | None = None,
) -> None:
    finding = {"code": code, "severity": severity, "action": "inspect_only"}
    if subject is not None:
        finding["subject"] = subject
    report["findings"].append(finding)


def _add_entry(report: dict, entry: dict) -> None:
    report["entries"].append({"action": "inspect_only", **entry})


def _is_global_stage_path(root: Path, path: str | Path) -> bool:
    return _lexically_within(path, _root_path(root) / "staging")


def _scan_tree(
    report: dict,
    root: Path,
    referenced_paths: set[str],
) -> None:
    versions_root = _root_path(root)
    for namespace in ("staging", "archive_work"):
        start = versions_root / namespace
        state = _path_state(versions_root, start, want_directory=True)
        if state == "missing":
            continue
        if state != "present_directory":
            if state == "unsafe_link":
                _add_entry(report, {"kind": "unsafe_link", "path": str(start)})
                _add_finding(report, "unsafe_link", subject={"path": str(start)})
            else:
                _add_finding(
                    report,
                    "staging_namespace_unavailable",
                    subject={"namespace": namespace, "state": state},
                )
            continue

        stack = [start]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as scanner:
                    children = sorted(scanner, key=lambda item: item.name)
            except OSError:
                _add_finding(
                    report,
                    "staging_namespace_unreadable",
                    subject={"path": str(directory)},
                )
                continue
            for child in children:
                child_path = Path(child.path)
                if _is_link_or_junction(child_path):
                    _add_entry(report, {"kind": "unsafe_link", "path": str(child_path)})
                    _add_finding(report, "unsafe_link", subject={"path": str(child_path)})
                    continue
                try:
                    if child.is_dir(follow_symlinks=False):
                        stack.append(child_path)
                    elif child.is_file(follow_symlinks=False):
                        registered = _path_key(child_path) in referenced_paths
                        _add_entry(
                            report,
                            {
                                "kind": "stage_file",
                                "path": str(child_path),
                                "state": "registered_reference" if registered else "unregistered",
                                "namespace": namespace,
                            },
                        )
                        if not registered:
                            _add_finding(
                                report,
                                "unregistered_stage_file",
                                subject={"path": str(child_path), "namespace": namespace},
                            )
                except OSError:
                    _add_finding(report, "staging_entry_unreadable", subject={"path": str(child_path)})


def _candidate_directory_entries(
    report: dict,
    root: Path,
    *,
    document_id: int,
    version_id: int,
    version_no: int,
) -> None:
    candidate_dir = _root_path(root) / str(document_id) / str(version_no)
    state = _path_state(root, candidate_dir, want_directory=True)
    subject = {"version_id": version_id, "document_id": document_id, "version_no": version_no}
    if state == "missing":
        _add_finding(report, "owned_candidate_directory_missing", subject=subject)
        return
    if state != "present_directory":
        _add_finding(
            report,
            "unsafe_owned_candidate_directory",
            subject={**subject, "state": state},
        )
        return

    try:
        with os.scandir(candidate_dir) as scanner:
            children = sorted(scanner, key=lambda item: item.name)
    except OSError:
        _add_finding(report, "owned_candidate_directory_unreadable", subject=subject)
        return
    found_candidate = False
    for child in children:
        child_path = Path(child.path)
        if _is_link_or_junction(child_path):
            _add_finding(
                report,
                "unsafe_owned_version_candidate",
                subject={**subject, "path": str(child_path)},
            )
            continue
        try:
            if not child.is_file(follow_symlinks=False):
                continue
        except OSError:
            _add_finding(
                report,
                "owned_candidate_metadata_unreadable",
                subject={**subject, "path": str(child_path)},
            )
            continue
        found_candidate = True
        _add_entry(
            report,
            {
                "kind": "owned_version_candidate",
                "path": str(child_path),
                "document_id": document_id,
                "version_id": version_id,
                "version_no": version_no,
                "declared_path_missing": True,
                "candidate_only": True,
            },
        )
        _add_finding(
            report,
            "owned_version_candidate",
            severity="info",
            subject={**subject, "path": str(child_path)},
        )
    if not found_candidate:
        _add_finding(report, "owned_candidate_directory_empty", severity="info", subject=subject)


def _collect_database_references(conn: sqlite3.Connection, versions_root: Path) -> dict:
    """Load optional tables and build relational/path reference indexes."""
    tables = _table_names(conn)
    permission_columns = _table_columns(conn, "document_permissions")
    version_columns = _table_columns(conn, "document_versions")
    permissions = _select_rows(conn, "document_permissions", permission_columns)
    versions = _select_rows(conn, "document_versions", version_columns)
    permission_by_id = {int(row["id"]): row for row in permissions}

    refs_by_version: dict[int, list[dict]] = defaultdict(list)
    if "current_version_id" in permission_columns:
        for permission in permissions:
            version_id = permission.get("current_version_id")
            if version_id is not None:
                refs_by_version[int(version_id)].append(
                    {
                        "kind": "permission_current_version",
                        "document_id": int(permission["id"]),
                        "kb_id": permission.get("kb_id"),
                        "pending": False,
                    }
                )

    kb_document_rows: list[dict] = []
    kb_document_columns: set[str] = set()
    if "kb_documents" in tables:
        kb_document_columns = _table_columns(conn, "kb_documents")
        kb_document_rows = _select_rows(conn, "kb_documents", kb_document_columns)
        for row in kb_document_rows:
            version_id = row.get("document_version_id")
            if version_id is not None:
                refs_by_version[int(version_id)].append(
                    {
                        "kind": "kb_document_version",
                        "document_id": row.get("document_id"),
                        "kb_id": row.get("kb_id"),
                        "status": row.get("status"),
                        "pending": str(row.get("status", "")).casefold()
                        in {"pending", "queued", "processing", "indexing"},
                    }
                )

    manifests: list[dict] = []
    manifest_columns: set[str] = set()
    if "material_archive_manifests" in tables:
        manifest_columns = _table_columns(conn, "material_archive_manifests")
        manifests = _select_rows(conn, "material_archive_manifests", manifest_columns)
        if {"id", "container_version_id"}.issubset(manifest_columns):
            for row in manifests:
                refs_by_version[int(row["container_version_id"])].append(
                    {
                        "kind": "archive_manifest_container",
                        "manifest_id": int(row["id"]),
                        "document_id": row.get("container_document_id"),
                        "status": row.get("status"),
                        "pending": str(row.get("status", "")).casefold() in _PENDING_MANIFEST_STATUSES,
                    }
                )

    jobs: list[dict] = []
    job_columns: set[str] = set()
    if "document_index_jobs" in tables:
        job_columns = _table_columns(conn, "document_index_jobs")
        jobs = _select_rows(conn, "document_index_jobs", job_columns)

    path_owners: dict[str, list[int]] = defaultdict(list)
    for row in versions:
        raw_path = row.get("file_path")
        if raw_path:
            path_owners[
                _path_key(raw_path if Path(str(raw_path)).is_absolute() else versions_root / str(raw_path))
            ].append(int(row["id"]))
    referenced_paths: set[str] = set()
    for row in versions:
        raw_path = row.get("file_path")
        if raw_path:
            path = raw_path if Path(str(raw_path)).is_absolute() else versions_root / str(raw_path)
            if _lexically_within(path, versions_root):
                referenced_paths.add(_path_key(path))
    if "source_path" in manifest_columns:
        for manifest in manifests:
            raw_path = manifest.get("source_path")
            if raw_path:
                path = raw_path if Path(str(raw_path)).is_absolute() else versions_root / str(raw_path)
                if _lexically_within(path, versions_root):
                    referenced_paths.add(_path_key(path))
    if "file_path" in kb_document_columns:
        for row in kb_document_rows:
            raw_path = row.get("file_path")
            if raw_path:
                path = raw_path if Path(str(raw_path)).is_absolute() else versions_root / str(raw_path)
                if _lexically_within(path, versions_root):
                    referenced_paths.add(_path_key(path))

    return {
        "permissions": permissions,
        "permission_by_id": permission_by_id,
        "versions": versions,
        "refs_by_version": refs_by_version,
        "kb_document_rows": kb_document_rows,
        "kb_document_columns": kb_document_columns,
        "manifests": manifests,
        "manifest_columns": manifest_columns,
        "jobs": jobs,
        "path_owners": path_owners,
        "referenced_paths": referenced_paths,
    }


def _version_path_state(
    report: dict,
    versions_root: Path,
    *,
    version: dict,
    permission: dict | None,
    document_id: int,
    version_id: int,
    status: str,
    declared_path: str,
) -> tuple[str, str, Path | None]:
    """Apply Task 3's owner proof before inspecting a version path."""
    kb_id = str(permission["kb_id"]) if permission and permission.get("kb_id") is not None else None
    legacy_stage = status in {"current", "historical"} and _is_global_stage_path(versions_root, declared_path)
    if permission is None or not kb_id:
        _add_finding(
            report,
            "version_owner_missing",
            subject={"version_id": version_id, "document_id": document_id},
        )
        return "unowned", "owner_unverified_uninspected", None

    try:
        resolved_path = resolve_owned_version_path(
            versions_root,
            str(version.get("file_path") or ""),
            document_id=document_id,
            kb_id=kb_id,
        )
    except ValueError:
        _add_finding(
            report,
            "version_path_unowned",
            subject={"version_id": version_id, "document_id": document_id},
        )
        if legacy_stage:
            state = _path_state(versions_root, declared_path)
            return "unowned", "present_legacy_committed_stage" if state == "present" else state, None
        state = _path_state(versions_root, declared_path)
        if state == "unsafe_link":
            return "unowned", state, None
        if state == "outside_root_uninspected":
            return "unowned", state, None
        return "unowned", "unowned_path_uninspected", None

    return "owned", _path_state(versions_root, resolved_path), resolved_path


def _diagnose_version(
    report: dict,
    versions_root: Path,
    version: dict,
    snapshot: dict,
) -> None:
    """Report one version's owner, references, file state, and material job state."""
    version_id = int(version["id"])
    document_id = int(version["document_id"])
    version_no = int(version["version_no"])
    status = str(version["status"])
    permission = snapshot["permission_by_id"].get(document_id)
    kb_id = str(permission["kb_id"]) if permission and permission.get("kb_id") is not None else None
    raw_path = str(version.get("file_path") or "")
    declared_path = raw_path if Path(raw_path).is_absolute() else str(versions_root / raw_path)
    references = list(snapshot["refs_by_version"].get(version_id, []))
    if permission is not None:
        references.append(
            {
                "kind": "document_permission_owner",
                "document_id": document_id,
                "kb_id": permission.get("kb_id"),
                "doc_name": permission.get("doc_name"),
                "pending": False,
            }
        )

    ownership, file_state, resolved_path = _version_path_state(
        report,
        versions_root,
        version=version,
        permission=permission,
        document_id=document_id,
        version_id=version_id,
        status=status,
        declared_path=declared_path,
    )
    if resolved_path is not None:
        snapshot["referenced_paths"].add(_path_key(resolved_path))

    path_key = _path_key(declared_path)
    shared_version_ids = sorted(
        other_id for other_id in snapshot["path_owners"].get(path_key, []) if other_id != version_id
    )
    if shared_version_ids:
        _add_finding(
            report,
            "version_path_shared",
            subject={"version_id": version_id, "shared_version_ids": shared_version_ids},
        )

    shared_references = [
        reference
        for reference in references
        if reference.get("document_id") is not None and int(reference["document_id"]) != document_id
    ]
    if shared_references:
        _add_finding(
            report,
            "version_shared_reference",
            subject={"version_id": version_id, "reference_count": len(shared_references)},
        )

    version_jobs = [
        job
        for job in snapshot["jobs"]
        if job.get("version_id") is not None
        and int(job["version_id"]) == version_id
        and job.get("job_scope", "material") == "material"
        and job.get("job_type", "material_index") == "material_index"
    ]
    active_jobs = [job for job in version_jobs if str(job.get("status", "")).casefold() in _ACTIVE_JOB_STATUSES]
    material_job_state = "missing" if not version_jobs else "active" if active_jobs else "present_terminal"
    if status == "staging" and material_job_state == "missing":
        _add_finding(
            report,
            "staging_version_missing_material_job",
            subject={
                "version_id": version_id,
                "document_id": document_id,
                "reference_count": len(references),
            },
        )
    if active_jobs:
        _add_finding(
            report,
            "material_job_pending",
            severity="info",
            subject={"version_id": version_id, "job_ids": [int(job["id"]) for job in active_jobs]},
        )

    _add_entry(
        report,
        {
            "kind": "document_version",
            "version_id": version_id,
            "document_id": document_id,
            "version_no": version_no,
            "kb_id": kb_id,
            "status": status,
            "original_filename": version.get("original_filename"),
            "declared_path": declared_path,
            "ownership": ownership,
            "file_state": file_state,
            "references": references,
            "shared_version_ids": shared_version_ids,
            "shared_reference_count": len(shared_references),
            "material_job": {
                "state": material_job_state,
                "statuses": [str(job.get("status", "")) for job in version_jobs],
            },
        },
    )

    legacy_stage = status in {"current", "historical"} and _is_global_stage_path(versions_root, declared_path)
    if ownership == "owned" and file_state == "missing":
        _add_finding(
            report,
            "version_file_missing",
            subject={"version_id": version_id, "document_id": document_id, "status": status},
        )
        if status == "staging":
            _candidate_directory_entries(
                report,
                versions_root,
                document_id=document_id,
                version_id=version_id,
                version_no=version_no,
            )
    elif file_state == "unsafe_link":
        _add_finding(
            report,
            "version_path_unsafe_link",
            subject={"version_id": version_id, "document_id": document_id},
        )
    elif legacy_stage:
        _add_finding(
            report,
            "legacy_committed_stage_preserve",
            severity="info",
            subject={"version_id": version_id, "status": status},
        )


def _diagnose_versions(report: dict, versions_root: Path, snapshot: dict) -> None:
    for version in snapshot["versions"]:
        _diagnose_version(report, versions_root, version, snapshot)


def _diagnose_pending_kb_documents(report: dict, snapshot: dict) -> None:
    for row in snapshot["kb_document_rows"]:
        state = str(row.get("status", "")).casefold()
        if state in {"pending", "queued", "processing", "indexing"}:
            _add_entry(
                report,
                {
                    "kind": "pending_kb_document",
                    "kb_document_id": row.get("id"),
                    "document_id": row.get("document_id"),
                    "document_version_id": row.get("document_version_id"),
                    "kb_id": row.get("kb_id"),
                    "status": row.get("status"),
                },
            )
            _add_finding(
                report,
                "pending_kb_document_reference",
                severity="info",
                subject={"kb_document_id": row.get("id"), "status": row.get("status")},
            )


def _diagnose_archive_manifests(report: dict, versions_root: Path, snapshot: dict) -> None:
    """Report archive ownership and pending/confirmed source-path facts."""
    versions_by_id = {int(row["id"]): row for row in snapshot["versions"]}
    for manifest in snapshot["manifests"]:
        manifest_id = int(manifest["id"])
        status = str(manifest.get("status", ""))
        container_document_id = manifest.get("container_document_id")
        container_version_id = manifest.get("container_version_id")
        entry = {
            "kind": "archive_manifest",
            "manifest_id": manifest_id,
            "container_document_id": container_document_id,
            "container_version_id": container_version_id,
            "status": status,
        }
        if container_version_id is not None:
            container_version = versions_by_id.get(int(container_version_id))
            if (
                container_version is None
                or container_document_id is None
                or int(container_version["document_id"]) != int(container_document_id)
            ):
                _add_finding(
                    report,
                    "archive_container_version_mismatch",
                    subject={"manifest_id": manifest_id},
                )

        source_path = manifest.get("source_path") if "source_path" in snapshot["manifest_columns"] else None
        if not source_path:
            entry["source_state"] = "not_recorded"
        else:
            source_path = str(source_path)
            full_source = source_path if Path(source_path).is_absolute() else str(versions_root / source_path)
            entry["source_path"] = full_source
            container_permission = (
                snapshot["permission_by_id"].get(int(container_document_id))
                if container_document_id is not None
                else None
            )
            source_kb_id = str(container_permission["kb_id"]) if container_permission else None
            hashed_stage_root = (
                _root_path(versions_root) / "staging" / "kb" / hashlib.sha256(source_kb_id.encode("utf-8")).hexdigest()
                if source_kb_id
                else None
            )
            source_owned = bool(hashed_stage_root and _lexically_within(full_source, hashed_stage_root))
            confirmed = status.casefold() == "confirmed"
            if not _lexically_within(full_source, versions_root):
                source_fs_state = "outside_root_uninspected"
            elif confirmed or source_owned:
                source_fs_state = _path_state(versions_root, full_source)
            else:
                source_fs_state = "unowned_path_uninspected"
            if confirmed and source_fs_state == "missing":
                entry["source_state"] = "disappeared_after_confirmation"
            elif not source_owned:
                entry["source_state"] = "unowned"
                _add_finding(
                    report,
                    "archive_source_unowned",
                    subject={"manifest_id": manifest_id, "status": status},
                )
            elif source_fs_state == "missing":
                entry["source_state"] = "missing"
                if status.casefold() in _PENDING_MANIFEST_STATUSES:
                    _add_finding(
                        report,
                        "pending_archive_source_missing",
                        subject={"manifest_id": manifest_id, "status": status},
                    )
            elif source_fs_state == "present":
                entry["source_state"] = "present"
            else:
                entry["source_state"] = source_fs_state
                _add_finding(
                    report,
                    "archive_source_unsafe_or_unavailable",
                    subject={"manifest_id": manifest_id, "state": source_fs_state},
                )
        _add_entry(report, entry)
        if status.casefold() in _PENDING_MANIFEST_STATUSES:
            _add_finding(
                report,
                "pending_archive_manifest",
                severity="info",
                subject={"manifest_id": manifest_id, "status": status},
            )


def _finalize_report(report: dict) -> dict:
    kind_counts = Counter(entry["kind"] for entry in report["entries"])
    code_counts = Counter(finding["code"] for finding in report["findings"])
    report["counts"] = {
        "entries": len(report["entries"]),
        "findings": len(report["findings"]),
        "by_kind": dict(sorted(kind_counts.items())),
        "by_code": dict(sorted(code_counts.items())),
    }
    return report


def _audit_connection(conn: sqlite3.Connection, database: Path, versions_root: Path) -> dict:
    report = _empty_report()
    report["inputs"] = {"database": str(database), "versions_root": str(versions_root)}
    unsupported = _schema_error(conn)
    if unsupported:
        report["error"] = {"code": "unsupported_schema", "message": unsupported}
        return report

    snapshot = _collect_database_references(conn, versions_root)
    _diagnose_versions(report, versions_root, snapshot)
    _diagnose_pending_kb_documents(report, snapshot)
    _diagnose_archive_manifests(report, versions_root, snapshot)
    _scan_tree(report, versions_root, snapshot["referenced_paths"])
    return _finalize_report(report)


def audit_staging(database: Path, versions_root: Path) -> dict:
    """Audit a closed, self-contained SQLite snapshot without changing either input."""
    database_path = _absolute(database)
    versions_path = _absolute(versions_root)
    report = _empty_report()
    report["inputs"] = {"database": str(database_path), "versions_root": str(versions_path)}

    if _has_link_component(database_path):
        return _error_report("database_link_unsupported", "database path crosses a symbolic link or junction")
    try:
        db_stat = database_path.lstat()
    except FileNotFoundError:
        return _error_report("database_not_found", "database snapshot does not exist")
    except OSError:
        return _error_report("database_unreadable", "database snapshot metadata is unavailable")
    if not stat.S_ISREG(db_stat.st_mode):
        return _error_report("database_not_regular", "database snapshot must be a regular file")
    if _has_link_component(versions_path):
        return _error_report("versions_root_link_unsupported", "versions root path crosses a symbolic link or junction")
    try:
        root_stat = versions_path.lstat()
    except FileNotFoundError:
        return _error_report("versions_root_not_found", "versions root does not exist")
    except OSError:
        return _error_report("versions_root_unreadable", "versions root metadata is unavailable")
    if not stat.S_ISDIR(root_stat.st_mode):
        return _error_report("versions_root_not_directory", "versions root must be a directory")

    sidecars = [Path(f"{database_path}-wal"), Path(f"{database_path}-shm")]
    if any(os.path.lexists(os.fspath(sidecar)) for sidecar in sidecars):
        return _error_report(
            "snapshot_sidecar_present",
            "database must be a closed self-contained snapshot without WAL or SHM sidecars",
        )

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(_sqlite_uri(database_path), uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        result = _audit_connection(conn, database_path, versions_path)
        return result
    except sqlite3.DatabaseError:
        return _error_report("invalid_sqlite_snapshot", "database snapshot could not be read as SQLite")
    finally:
        if conn is not None:
            conn.close()
