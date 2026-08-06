"""Idempotent startup migration for material metadata.

Backfills the material FTS rows for documents that predate the material
lifecycle. It initializes missing logical metadata to safe defaults without
changing ACL, owner, or current version, indexes only metadata (never
re-embedding existing content), and commits each logical document
independently so one corrupt record cannot roll back completed records.
"""

from __future__ import annotations

from dataclasses import dataclass

from rag.user_db import UserDB


@dataclass(frozen=True)
class MaterialMigrationReport:
    migrated_documents: int
    missing_sources: int
    unsupported_sources: int


def migrate_material_metadata(db: UserDB) -> MaterialMigrationReport:
    migrated = 0
    missing = 0
    unsupported = 0
    for doc in db.list_document_permissions_all():
        current_version_id = doc.get("current_version_id")
        if current_version_id is None:
            missing += 1
            continue
        with db._lock:
            exists = db._conn.execute(
                "SELECT 1 FROM material_search_fts "
                "WHERE document_id = ? AND version_id = ? LIMIT 1",
                (doc["id"], current_version_id),
            ).fetchone()
            if exists is not None:
                continue
            # Initialize logical metadata defaults; never overwrite real values.
            db._conn.execute(
                "UPDATE document_permissions "
                "SET logical_path=COALESCE(NULLIF(logical_path, ''), ''), "
                "document_type=COALESCE(NULLIF(document_type, ''), ''), "
                "tags_json=COALESCE(NULLIF(tags_json, '[]'), '[]') "
                "WHERE id = ?",
                (doc["id"],),
            )
            full = db._conn.execute(
                "SELECT doc_name, logical_path, document_type, tags_json "
                "FROM document_permissions WHERE id = ?",
                (doc["id"],),
            ).fetchone()
            db._conn.execute(
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
            db._conn.commit()
        migrated += 1
    return MaterialMigrationReport(migrated_documents=migrated, missing_sources=missing, unsupported_sources=unsupported)
