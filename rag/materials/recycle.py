"""Tombstone-first deletion, restore, and idempotent purge.

Deletion never physically removes rows. ``delete_to_recycle`` sets a
tombstone and downgrades the current version in one transaction; every read
gate rejects ``deleted_at IS NOT NULL`` synchronously. ``restore_from_recycle``
reverses the tombstone before the 30-day deadline. ``purge_document`` removes
derived artifacts and immutable version files after the deadline while
preserving document/version identity, uploader, timestamps, hashes, and the
minimal citation audit (with no text preview).

``scan_due_purges`` is the 30-day sweep: it enqueues one ``purge_document``
row per expired tombstone in the existing ``document_index_jobs`` queue, so the
``MaterialWorker`` lease/retry machinery performs the actual purge. Enqueueing
is idempotent (an active or recently failed job suppresses a duplicate) which
keeps the per-tick scan cheap and lets tombstones created before this sweep
existed still be collected.
"""

from __future__ import annotations

import os
import stat
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from rag.kb_lifecycle import KBLifecycleError, assert_kb_available_in_tx
from rag.materials.org_audit import record_org_material_audit
from rag.user_db import UserDB

RESTORE_WINDOW_SECONDS = 30 * 86400
PURGED_STATE = "purged_by_retention_policy"
PURGE_JOB_TYPE = "purge_document"
#: A failed purge is retried at most once per hour instead of once per scan tick.
PURGE_RETRY_COOLDOWN_SECONDS = 3600.0


class RecycleError(Exception):
    """Raised for an illegal delete/restore/purge transition."""

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        super().__init__(f"recycle rejected: {reason_code}: {detail}")


@dataclass(frozen=True)
class RecycleRecord:
    document_id: int
    status: str
    metadata_version: int
    restore_until: float


@dataclass(frozen=True)
class RestoreResult:
    document_id: int
    status: str
    metadata_version: int


@dataclass(frozen=True)
class PurgeResult:
    document_id: int
    status: str
    removed_bytes: int
    version_ids: tuple[int, ...] = ()


def _require_edit(db: UserDB, document_id: int, actor: dict) -> dict:
    doc = db._conn.execute(
        "SELECT * FROM document_permissions WHERE id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        raise RecycleError("document_missing")
    if not (actor.get("is_admin") or doc["owner_id"] == actor["id"]):
        raise RecycleError("no_edit_permission")
    return dict(doc)


def _append_audit(db: UserDB, document_id: int, actor_id: int, action: str, now: float) -> None:
    db._conn.execute(
        "INSERT INTO material_audit_log (document_id, actor_id, action, created_at) "
        "VALUES (?, ?, ?, ?)",
        (document_id, actor_id, action, now),
    )


def delete_to_recycle(
    db: UserDB, document_id: int, actor: dict, now: float | None = None,
    *, organization_authorized: bool = False,
) -> RecycleRecord:
    """Tombstone a document and downgrade its current version atomically."""
    now = now if now is not None else time.time()
    with db._lock, db.transaction(immediate=True):
        if organization_authorized:
            row = db._conn.execute(
                "SELECT * FROM document_permissions WHERE id = ?", (document_id,)
            ).fetchone()
            if row is None:
                raise RecycleError("document_missing")
            doc = dict(row)
        else:
            doc = _require_edit(db, document_id, actor)
        assert_kb_available_in_tx(db._conn, str(doc["kb_id"]))
        if doc["deleted_at"] is not None:
            raise RecycleError("already_deleted")
        expected = doc["metadata_version"]
        updated = db._conn.execute(
            "UPDATE document_permissions "
            "SET deleted_at=?, deleted_by=?, restore_until=?, purge_state='', "
            "metadata_version=metadata_version+1 "
            "WHERE id=? AND metadata_version=?",
            (now, actor["id"], now + RESTORE_WINDOW_SECONDS, document_id, expected),
        )
        if updated.rowcount != 1:
            raise RecycleError("metadata_conflict")
        if doc["current_version_id"] is not None:
            db._conn.execute(
                "UPDATE document_versions SET status='recycle_pending' WHERE id=?",
                (doc["current_version_id"],),
            )
        _append_audit(db, document_id, actor["id"], "delete_to_recycle", now)
        record_org_material_audit(db, actor, "document_recycled", str(doc["kb_id"]))
    return RecycleRecord(
        document_id=document_id,
        status="deleted",
        metadata_version=expected + 1,
        restore_until=now + RESTORE_WINDOW_SECONDS,
    )


def restore_from_recycle(
    db: UserDB,
    document_id: int,
    actor: dict,
    expected_metadata_version: int,
    now: float | None = None,
    *,
    organization_authorized: bool = False,
) -> RestoreResult:
    """Reverse a tombstone before the deadline; only deleter/owner/admin."""
    now = now if now is not None else time.time()
    with db._lock, db.transaction(immediate=True):
        doc = db._conn.execute(
            "SELECT * FROM document_permissions WHERE id = ?", (document_id,)
        ).fetchone()
        if doc is None:
            raise RecycleError("document_missing")
        assert_kb_available_in_tx(db._conn, str(doc["kb_id"]))
        if doc["deleted_at"] is None:
            raise RecycleError("not_deleted")
        if now > (doc["restore_until"] or 0):
            raise RecycleError("restore_window_expired")
        if not organization_authorized and not (
            actor.get("is_admin") or actor["id"] in (doc["owner_id"], doc["deleted_by"])
        ):
            raise RecycleError("no_restore_permission")
        updated = db._conn.execute(
            "UPDATE document_permissions "
            "SET deleted_at=NULL, deleted_by=NULL, restore_until=NULL, purge_state='', "
            "metadata_version=metadata_version+1 "
            "WHERE id=? AND metadata_version=?",
            (document_id, expected_metadata_version),
        )
        if updated.rowcount != 1:
            raise RecycleError("metadata_conflict")
        if doc["current_version_id"] is not None:
            db._conn.execute(
                "UPDATE document_versions SET status='current' "
                "WHERE id=? AND status='recycle_pending'",
                (doc["current_version_id"],),
            )
        # A purge enqueued by the sweep must never touch a restored document.
        with suppress(Exception):
            db._conn.execute(
                "UPDATE document_index_jobs SET status='cancelled', lease_owner=NULL, "
                "lease_expires_at=NULL, updated_at=? "
                "WHERE document_id=? AND job_type=? AND job_scope='material' "
                "AND status IN ('queued','retry','running')",
                (now, document_id, PURGE_JOB_TYPE),
            )
        _append_audit(db, document_id, actor["id"], "restore_from_recycle", now)
        record_org_material_audit(db, actor, "document_restored", str(doc["kb_id"]))
    return RestoreResult(document_id=document_id, status="restored", metadata_version=expected_metadata_version + 1)


def purge_document(
    db: UserDB,
    document_id: int,
    now: float | None = None,
    *,
    vector_store: object | None = None,
    versions_root: Path | None = None,
    structured_datasets_root: Path | None = None,
) -> PurgeResult:
    """Purge derived artifacts and immutable files after the deadline.

    Idempotent: a second purge returns ``already_purged`` with zero bytes.
    Identity rows, version rows, uploader, timestamps, hashes, and citation
    state are preserved; the purged text preview is never kept.
    """
    now = now if now is not None else time.time()
    removed_bytes = 0
    version_files: list[Path] = []
    dataset_files: list[Path] = []
    version_ids: list[int] = []
    with db._lock, db.transaction(immediate=True):
        doc = db._conn.execute(
            "SELECT * FROM document_permissions WHERE id = ?", (document_id,)
        ).fetchone()
        if doc is None:
            raise RecycleError("document_missing")
        if doc["purge_state"] == PURGED_STATE:
            return PurgeResult(document_id, "already_purged", 0)
        if doc["deleted_at"] is None:
            raise RecycleError("not_tombstoned")
        if now <= (doc["restore_until"] or 0):
            raise RecycleError("restore_window_open")

        version_files, dataset_files, version_ids, removed_bytes = _plan_purge_files(
            db, document_id, versions_root, structured_datasets_root
        )

    for path in [*version_files, *dataset_files]:
        path.unlink(missing_ok=True)
    if vector_store is not None:
        vector_store.delete_document(document_id)

    # Keep the tombstone, version paths, and dataset rows intact until every
    # external cleanup succeeds. A retry can then find artifacts after a partial
    # unlink or a transient vector-store failure.
    with db._lock, db.transaction(immediate=True):
        doc = db._conn.execute(
            "SELECT * FROM document_permissions WHERE id = ?", (document_id,)
        ).fetchone()
        if doc is None:
            raise RecycleError("document_missing")
        if doc["purge_state"] == PURGED_STATE:
            return PurgeResult(document_id, "already_purged", removed_bytes)
        if doc["deleted_at"] is None:
            raise RecycleError("not_tombstoned")
        if now <= (doc["restore_until"] or 0):
            raise RecycleError("restore_window_open")

        db._conn.execute(
            "UPDATE document_versions SET status='purged' WHERE document_id = ?",
            (document_id,),
        )
        db._conn.execute(
            "UPDATE document_permissions SET current_version_id=NULL, purge_state=?, "
            "metadata_version=metadata_version+1 WHERE id = ?",
            (PURGED_STATE, document_id),
        )
        db._conn.execute(
            "DELETE FROM material_search_fts WHERE document_id = ?", (document_id,)
        )
        db._conn.execute(
            "DELETE FROM material_unit_fingerprints WHERE document_id = ?", (document_id,)
        )
        db._conn.execute(
            "DELETE FROM structured_datasets WHERE document_id = ?", (document_id,)
        )
        with suppress(Exception):
            db._conn.execute(
                "DELETE FROM vector_flag_repairs WHERE document_id = ?", (document_id,)
            )
        db._conn.execute(
            "UPDATE answer_citations SET state=?, text_preview='' "
            "WHERE source_kind='document' AND document_id = ?",
            (PURGED_STATE, document_id),
        )
        _append_audit(db, document_id, 0, "purge", now)
    if versions_root is not None:
        _prune_empty_dirs(versions_root / str(document_id))
    if structured_datasets_root is not None:
        _prune_empty_dirs(structured_datasets_root / str(document_id))

    return PurgeResult(
        document_id=document_id,
        status="purged",
        removed_bytes=removed_bytes,
        version_ids=tuple(version_ids),
    )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _check_no_links(path: Path) -> None:
    # Check the root itself and its parents too, including Windows junctions.
    for component in reversed((path, *path.parents)):
        try:
            if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                raise RecycleError("purge_path_unsafe_link")
        except OSError as exc:
            raise RecycleError("purge_path_invalid") from exc


def _path_options(root: Path, raw: str | Path) -> list[Path]:
    root = _absolute_path(Path(root))
    path = Path(raw)
    if ".." in path.parts:
        raise RecycleError("purge_path_outside_root")
    candidates = [_absolute_path(path)] if path.is_absolute() else [
        _absolute_path(root / path), _absolute_path(path)
    ]
    options = []
    for candidate in candidates:
        if candidate != root and candidate.is_relative_to(root) and candidate not in options:
            options.append(candidate)
    if not options:
        raise RecycleError("purge_path_outside_root")
    return options


def _candidate(root: Path, raw: str | Path) -> Path:
    options = _path_options(root, raw)
    existing = []
    for path in options:
        _check_no_links(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RecycleError("purge_path_invalid") from exc
        if not stat.S_ISREG(info.st_mode):
            raise RecycleError("purge_path_invalid")
        existing.append(path)
    if len(existing) > 1:
        raise RecycleError("purge_path_ambiguous")
    return existing[0] if existing else options[0]


def _plan_purge_files(db: UserDB, document_id: int, versions_root: Path | None,
                      datasets_root: Path | None) -> tuple[list[Path], list[Path], list[int], int]:
    """Validate every deletion target and reference before any external effect."""
    from rag.spreadsheets.store import resolve_dataset_path

    versions = db._conn.execute(
        "SELECT id, file_path FROM document_versions WHERE document_id=?", (document_id,)
    ).fetchall()
    ids = [int(row["id"]) for row in versions]
    version_files: list[Path] = []
    for row in versions:
        raw = str(row["file_path"] or "")
        if not raw:
            raise RecycleError("purge_path_invalid")
        if versions_root is None:
            # Without a root even a relative retry target cannot be located safely.
            raise RecycleError("purge_versions_root_missing")
        version_files.append(_candidate(versions_root, raw))

    datasets = db._conn.execute(
        "SELECT document_version_id FROM structured_datasets WHERE document_id=?", (document_id,)
    ).fetchall()
    if datasets and datasets_root is None:
        raise RecycleError("purge_datasets_root_missing")
    dataset_files: list[Path] = []
    for row in datasets:
        version_id = int(row["document_version_id"])
        if version_id not in ids:
            raise RecycleError("purge_dataset_identity_mismatch")
        if datasets_root is not None:
            try:
                path = resolve_dataset_path(_absolute_path(datasets_root), document_id, version_id)
            except ValueError as exc:
                raise RecycleError("purge_dataset_identity_mismatch") from exc
            dataset_files.append(_candidate(datasets_root, path))

    foreign_datasets = db._conn.execute(
        "SELECT document_id, document_version_id FROM structured_datasets WHERE document_id<>?",
        (document_id,),
    ).fetchall()
    if foreign_datasets and datasets_root is None:
        raise RecycleError("purge_datasets_root_missing")
    dataset_references = []
    for row in foreign_datasets:
        try:
            dataset_references.append(resolve_dataset_path(
                _absolute_path(datasets_root), int(row["document_id"]), int(row["document_version_id"])
            ))
        except (TypeError, ValueError) as exc:
            raise RecycleError("purge_dataset_identity_mismatch") from exc

    targets = {os.path.normcase(str(path)) for path in [*version_files, *dataset_files]}
    if targets:
        references = db._conn.execute(
            "SELECT file_path FROM document_versions WHERE document_id<>?", (document_id,)
        ).fetchall()
        sources = db._conn.execute(
            "SELECT source_path FROM material_archive_manifests WHERE container_document_id<>?", (document_id,)
        ).fetchall()
        for reference_value in [*(row[0] for row in references), *(row[0] for row in sources), *dataset_references]:
            raw = str(reference_value or "")
            if not raw:
                continue
            reference = Path(raw)
            if not reference.is_absolute() and versions_root is None:
                raise RecycleError("purge_versions_root_missing")
            options = [_absolute_path(reference)]
            if not reference.is_absolute():
                options.append(_absolute_path(Path(versions_root) / reference))
            for path in options:
                key = os.path.normcase(str(path))
                try:
                    resolved_key = os.path.normcase(str(path.resolve()))
                except (OSError, RuntimeError) as exc:
                    raise RecycleError("purge_path_invalid") from exc
                if key in targets or resolved_key in targets:
                    raise RecycleError("purge_path_shared")

    existing_versions = [path for path in version_files if path.is_file()]
    existing_datasets = [path for path in dataset_files if path.is_file()]
    total = 0
    for path in [*existing_versions, *existing_datasets]:
        with suppress(OSError):
            total += path.stat().st_size
    return existing_versions, existing_datasets, ids, total


def _has_active_purge_job(db: UserDB, document_id: int, now: float, cooldown: float) -> bool:
    row = db._conn.execute(
        "SELECT 1 FROM document_index_jobs "
        "WHERE document_id = ? AND job_type = ? "
        "AND (status IN ('queued','retry','running') "
        "     OR (status = 'failed' AND updated_at > ?)) LIMIT 1",
        (document_id, PURGE_JOB_TYPE, now - cooldown),
    ).fetchone()
    return row is not None


def enqueue_purge_job(
    db: UserDB,
    document_id: int,
    now: float | None = None,
    *,
    retry_cooldown: float = PURGE_RETRY_COOLDOWN_SECONDS,
) -> int | None:
    """Queue one purge job for a tombstoned document.

    Idempotent: an already queued/running purge, or a recently failed one,
    suppresses a duplicate so the periodic sweep cannot build a retry storm.
    Returns the new job id, or ``None`` when a job already exists.
    """
    now = now if now is not None else time.time()
    with db._lock, db.transaction(immediate=True):
        if _has_active_purge_job(db, document_id, now, retry_cooldown):
            return None
        version = db._conn.execute(
            "SELECT current_version_id, kb_id FROM document_permissions WHERE id = ?",
            (document_id,),
        ).fetchone()
        if version is None:
            return None
        try:
            assert_kb_available_in_tx(db._conn, str(version["kb_id"]))
        except KBLifecycleError:
            return None
        cursor = db._conn.execute(
            "INSERT INTO document_index_jobs "
            "(document_id, version_id, content_hash, job_type, status, available_at, "
            " created_at, updated_at) "
            "VALUES (?, ?, '', ?, 'queued', ?, ?, ?)",
            (
                document_id,
                int(version["current_version_id"] or 0),
                PURGE_JOB_TYPE,
                now,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid) if cursor.rowcount == 1 else None


def scan_due_purges(
    db: UserDB,
    now: float | None = None,
    limit: int = 100,
    *,
    retry_cooldown: float = PURGE_RETRY_COOLDOWN_SECONDS,
) -> list[int]:
    """Enqueue purge jobs for tombstones whose 30-day restore window has closed."""
    now = now if now is not None else time.time()
    with db.locked_read() as conn:
        rows = conn.execute(
            "SELECT id FROM document_permissions "
            "WHERE deleted_at IS NOT NULL AND restore_until IS NOT NULL "
            "AND restore_until <= ? AND COALESCE(purge_state, '') <> ? "
            "ORDER BY restore_until LIMIT ?",
            (now, PURGED_STATE, limit),
        ).fetchall()
    job_ids: list[int] = []
    for row in rows:
        job_id = enqueue_purge_job(
            db, int(row["id"]), now, retry_cooldown=retry_cooldown
        )
        if job_id is not None:
            job_ids.append(job_id)
    return job_ids


def _prune_empty_dirs(root: Path) -> None:
    # Never recurse through a link, even when an unrelated child is present.
    try:
        _check_no_links(_absolute_path(root))
    except RecycleError:
        return
    pending = [root]
    directories = []
    while pending:
        directory = pending.pop()
        try:
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                continue
            if not directory.is_dir():
                continue
            directories.append(directory)
            pending.extend(directory.iterdir())
        except OSError:
            continue
    for directory in reversed(directories):
        with suppress(OSError):
            directory.rmdir()
