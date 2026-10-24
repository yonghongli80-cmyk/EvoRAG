"""Idempotent startup migration for material metadata.

Backfills the material FTS rows for documents that predate the material
lifecycle. It initializes missing logical metadata to safe defaults without
changing ACL, owner, or current version, indexes only metadata (never
re-embedding existing content), and commits each logical document
independently so one corrupt record cannot roll back completed records.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rag.kb_lifecycle import KBLifecycleError, assert_kb_available_in_tx, kb_mutation
from rag.user_db import UserDB


@dataclass(frozen=True)
class MaterialMigrationReport:
    migrated_documents: int
    missing_sources: int
    unsupported_sources: int
    lifecycle_skipped: int = 0


def migrate_material_metadata(db: UserDB) -> MaterialMigrationReport:
    """Backfill FTS from a synchronous worker, even when startup calls us async."""
    try:
        in_async_task = asyncio.current_task() is not None
    except RuntimeError:
        in_async_task = False
    if in_async_task:
        # Startup currently invokes this synchronous migration from lifespan.
        # Move the complete per-document gated pass to a worker thread rather
        # than acquiring a permit from the event-loop task.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="material-migration") as pool:
            return pool.submit(_migrate_material_metadata, db).result()
    return _migrate_material_metadata(db)


def _migrate_material_metadata(db: UserDB) -> MaterialMigrationReport:
    migrated = 0
    missing = 0
    unsupported = 0
    lifecycle_skipped = 0
    for doc in db.list_document_permissions_all():
        current_version_id = doc.get("current_version_id")
        if current_version_id is None:
            missing += 1
            continue
        try:
            with kb_mutation(db, str(doc["kb_id"])), db._lock, db.transaction(immediate=True) as conn:
                assert_kb_available_in_tx(conn, str(doc["kb_id"]))
                exists = conn.execute(
                    "SELECT 1 FROM material_search_fts "
                    "WHERE document_id = ? AND version_id = ? LIMIT 1",
                    (doc["id"], current_version_id),
                ).fetchone()
                if exists is not None:
                    continue
                # Initialize logical metadata defaults; never overwrite real values.
                conn.execute(
                    "UPDATE document_permissions "
                    "SET logical_path=COALESCE(NULLIF(logical_path, ''), ''), "
                    "document_type=COALESCE(NULLIF(document_type, ''), ''), "
                    "tags_json=COALESCE(NULLIF(tags_json, '[]'), '[]') "
                    "WHERE id = ?",
                    (doc["id"],),
                )
                full = conn.execute(
                    "SELECT doc_name, logical_path, document_type, tags_json "
                    "FROM document_permissions WHERE id = ?",
                    (doc["id"],),
                ).fetchone()
                conn.execute(
                    "INSERT INTO material_search_fts "
                    "(document_id, version_id, filename, logical_path, document_type, tags, extracted_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, '')",
                    (
                        doc["id"],
                        current_version_id,
                        full["doc_name"],
                        full["logical_path"] or "",
                        full["document_type"] or "",
                        full["tags_json"] or "[]",
                    ),
                )
            migrated += 1
        except KBLifecycleError:
            lifecycle_skipped += 1
    return MaterialMigrationReport(
        migrated_documents=migrated,
        missing_sources=missing,
        unsupported_sources=unsupported,
        lifecycle_skipped=lifecycle_skipped,
    )
