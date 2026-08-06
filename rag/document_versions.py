"""Single versioned ingest service and duplicate-resolution contract.

Every upload entry point constructs a ``SourceObject`` and calls
``ingest_document_version`` so duplicate handling, immutable versions, and
atomic activation stay in one place.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rag.document_sources.base import SourceObject
from rag.document_version_store import (
    final_version_path,
    promote_version,
    stage_version,
)
from rag.knowledge_base import KnowledgeBaseManager
from rag.loader import load as load_document
from rag.user_db import UserDB

logger = logging.getLogger(__name__)

DuplicateAction = Literal["reject", "overwrite", "keep"]


class DuplicateDocument(Exception):
    """Raised when the default ``reject`` action meets an existing document."""

    def __init__(self, document_id: int, filename: str, current_version: int):
        self.document_id = document_id
        self.filename = filename
        self.current_version = current_version
        super().__init__(f"duplicate document {filename}")


@dataclass(frozen=True)
class IngestResult:
    document_id: int
    version_id: int
    filename: str
    version_no: int
    status: str
    chunks: int


def choose_document_name(
    db: UserDB, kb_id: str, filename: str, action: DuplicateAction
) -> str:
    """Resolve the final document name for a duplicate action."""
    existing = db.get_document_permission(filename, kb_id)
    if existing is None:
        return filename
    if action == "reject":
        current = db.get_current_document_version(existing["id"])
        raise DuplicateDocument(existing["id"], filename, current["version_no"])
    if action == "overwrite":
        return filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 1
    while db.get_document_permission(f"{stem} ({index}){suffix}", kb_id):
        index += 1
    return f"{stem} ({index}){suffix}"


def ingest_document_version(
    db: UserDB,
    manager: KnowledgeBaseManager,
    source: SourceObject,
    kb_id: str,
    actor: dict,
    duplicate_action: DuplicateAction,
    root: Path,
) -> IngestResult:
    """Ingest one source object as an immutable document version.

    - Overwriting never changes the document owner.
    - Identical-content overwrite returns ``status="unchanged"`` and creates
      no new version.
    - ``reject`` raises :class:`DuplicateDocument`.
    - On failure only the newly written vectors and reserved rows are removed.
    """
    proposed = source.display_name
    existing = db.get_document_permission(proposed, kb_id)

    # Identical-content overwrite never creates a new version.
    if existing is not None and duplicate_action == "overwrite":
        current = db.get_current_document_version(existing["id"])
        if current is not None and current["content_hash"] == source.content_hash:
            return IngestResult(
                existing["id"],
                current["id"],
                proposed,
                current["version_no"],
                "unchanged",
                0,
            )

    filename = choose_document_name(db, kb_id, proposed, duplicate_action)
    existing = db.get_document_permission(filename, kb_id)

    if existing is None:
        document_id = db.create_document_permission(filename, kb_id, actor["id"], 1, False)
        was_existing = False
    else:
        document_id = existing["id"]
        was_existing = True

    stage = stage_version(root, source)
    version_id = None
    version_no = 0
    indexed = False
    try:
        load_document(str(stage))
        version_id = db.create_document_version(
            document_id, filename, str(stage), source.content_hash, actor["id"]
        )
        version = db.get_document_version_by_id(version_id)
        version_no = version["version_no"]
        count = manager.add_document(
            kb_id,
            str(stage),
            doc_name=filename,
            doc_permission_id=document_id,
            document_id=document_id,
            document_version_id=version_id,
            version_no=version_no,
        )
        indexed = True
        final_path = final_version_path(root, document_id, version_no, filename)
        promote_version(stage, final_path)
        db.set_version_file_path(version_id, str(final_path))
        db.activate_document_version(document_id, version_id, kb_id, filename)
    except DuplicateDocument:
        raise
    except Exception:
        if indexed and not was_existing:
            try:
                manager.remove_document(kb_id, filename)
            except Exception:
                logger.exception("ingest vector rollback failed")
        if version_id is not None:
            try:
                db.delete_document_version(version_id)
            except Exception:
                logger.exception("ingest version row rollback failed")
        if not was_existing:
            try:
                db.delete_document_permission(document_id)
            except Exception:
                logger.exception("ingest permission rollback failed")
        logger.exception(
            "versioned ingest failed",
            extra={"kb_id": kb_id, "document_filename": filename},
        )
        raise
    finally:
        stage.unlink(missing_ok=True)

    status = "overwritten" if was_existing else "created"
    return IngestResult(
        document_id, version_id, filename, version_no, status, count
    )


@dataclass(frozen=True)
class MigrationReport:
    migrated: int
    missing: int
    unchanged: int


def migrate_existing_document_versions(
    db: UserDB, versions_root: Path, default_upload_root: Path
) -> MigrationReport:
    """Create version-1 rows for documents created before versioning.

    Legacy sources are copied, never moved or deleted. Each document commits
    independently so one corrupt record cannot roll back completed records.
    Missing sources become a ``failed`` version.
    """
    migrated = 0
    missing = 0
    unchanged = 0
    for document in db.list_document_permissions_all():
        if document.get("current_version_id") is not None:
            unchanged += 1
            continue

        document_id = document["id"]
        filename = document["doc_name"]
        owner_id = document["owner_id"]

        source = _resolve_legacy_source(db, document, default_upload_root)
        if source is None:
            version_id = db.create_document_version(
                document_id, filename, str(versions_root / "missing"), "missing", owner_id
            )
            db.set_version_status(version_id, "failed")
            missing += 1
            logger.info(
                "migration: missing source for document",
                extra={"document_id": document_id, "document_filename": filename},
            )
            continue

        final_path = final_version_path(versions_root, document_id, 1, filename)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, final_path)
        digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
        version_id = db.create_document_version(
            document_id, filename, str(final_path), digest, owner_id
        )
        db.activate_document_version(document_id, version_id, document["kb_id"], filename)
        migrated += 1

    return MigrationReport(migrated=migrated, missing=missing, unchanged=unchanged)


def _resolve_legacy_source(db: UserDB, document: dict, default_upload_root: Path) -> Path | None:
    """Resolve a legacy source file from kb_documents or the upload root."""
    kb_document = db.get_kb_document(document["kb_id"], document["doc_name"])
    if kb_document and kb_document.get("file_path"):
        candidate = Path(kb_document["file_path"])
        if candidate.is_file():
            return candidate
    candidate = default_upload_root / document["doc_name"]
    if candidate.is_file():
        return candidate
    return None
