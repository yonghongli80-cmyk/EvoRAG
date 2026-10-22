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

import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

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
                "WHERE document_id=? AND job_type=? "
                "AND status IN ('queued','retry','running')",
                (now, document_id, PURGE_JOB_TYPE),
            )
        _append_audit(db, document_id, actor["id"], "restore_from_recycle", now)
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

        versions = db._conn.execute(
            "SELECT id, file_path FROM document_versions WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        for version in versions:
            version_ids.append(int(version["id"]))
            path = Path(version["file_path"])
            if path.is_file():
                with suppress(OSError):
                    removed_bytes += path.stat().st_size
                version_files.append(path)

        dataset_files, dataset_bytes = _dataset_files(
            db, document_id, structured_datasets_root
        )
        removed_bytes += dataset_bytes

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
            "UPDATE answer_citations SET state=?, text_preview='' WHERE document_id = ?",
            (PURGED_STATE, document_id),
        )
        _append_audit(db, document_id, 0, "purge", now)

    for path in [*version_files, *dataset_files]:
        with suppress(OSError):
            path.unlink(missing_ok=True)
    if vector_store is not None:
        with suppress(Exception):
            vector_store.delete_document(document_id)
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


def _dataset_files(
    db: UserDB, document_id: int, structured_datasets_root: Path | None
) -> tuple[list[Path], int]:
    """Return the immutable dataset files and their total size for a document."""
    if structured_datasets_root is None:
        return [], 0
    from rag.spreadsheets.store import resolve_dataset_path

    rows = db._conn.execute(
        "SELECT document_version_id FROM structured_datasets WHERE document_id = ?",
        (document_id,),
    ).fetchall()
    files: list[Path] = []
    total = 0
    for row in rows:
        with suppress(ValueError):
            path = resolve_dataset_path(
                structured_datasets_root, document_id, int(row["document_version_id"])
            )
            if path.is_file():
                with suppress(OSError):
                    total += path.stat().st_size
                files.append(path)
    return files, total


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
            "SELECT current_version_id FROM document_permissions WHERE id = ?",
            (document_id,),
        ).fetchone()
        if version is None:
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
    with suppress(OSError):
        for child in reversed(list(root.rglob("*"))):
            if child.is_dir():
                child.rmdir()
        if root.is_dir():
            root.rmdir()
