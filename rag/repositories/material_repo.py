"""Material index jobs, archive manifests, FTS, and organization proposals repository."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rag.kb_lifecycle import assert_kb_available_in_tx
from rag.repositories.base import BaseRepository, VersionConflict

logger = logging.getLogger(__name__)

#: FTS5 reports an invalid ``MATCH`` expression through ``OperationalError``.
#: These fragments identify a *query* error (the caller's text), which is the
#: only class of failure the repository may turn into "no hits"; anything else
#: (locked database, missing table, corrupt index) must reach the caller.
_FTS_SYNTAX_ERROR_HINTS = (
    "fts5: syntax error",
    "unterminated string",
    "unknown special query",
    "no such column",
    "unrecognized token",
    "malformed match",
)


def _is_fts_syntax_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in _FTS_SYNTAX_ERROR_HINTS)


def _fts_phrase_query(text: str) -> str:
    """Turn free user text into a safe FTS5 expression.

    Every whitespace-separated token is wrapped in quotes (the FTS5 string
    syntax) and embedded double quotes are doubled, so operators such as
    ``AND``/``OR``/``NOT``/``NEAR``, wildcards (``*``), column filters (``:``),
    parentheses, and punctuation like ``C++`` are searched literally instead of
    being parsed as query syntax.  Tokens are joined by whitespace, which FTS5
    treats as an implicit ``AND`` — the same ranking semantics a raw multi-word
    query had before escaping.
    """
    phrases = []
    for token in text.split():
        escaped = token.replace('"', '""')
        phrases.append(f'"{escaped}"')
    return " ".join(phrases)


#: D-10 rollback payload.  The baseline schema creates the batch-items table;
#: these columns record what an organization batch changed so a rollback can put
#: tags and document_type back, not only the logical path.
_ORGANIZATION_BATCH_ITEM_PAYLOAD_COLUMNS = (
    ("before_tags_json", "TEXT"),
    ("after_tags_json", "TEXT"),
    ("before_document_type", "TEXT"),
    ("after_document_type", "TEXT"),
)


def ensure_organization_batch_item_columns(conn: sqlite3.Connection) -> None:
    """Widen an already-created database with the D-10 rollback payload columns.

    New databases get the columns from their baseline schema; this compatibility
    step exists for databases created before the fix.  It is idempotent and
    tolerates a concurrent creator (SQLite reports ``duplicate column name``).
    """
    existing = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(material_organization_batch_items)")
    }
    if not existing:  # table not installed yet; the caller's insert will decide
        return
    for name, sql_type in _ORGANIZATION_BATCH_ITEM_PAYLOAD_COLUMNS:
        if name in existing:
            continue
        try:
            conn.execute(
                f"ALTER TABLE material_organization_batch_items ADD COLUMN {name} {sql_type}"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


class MaterialRepository(BaseRepository):
    """Handles index jobs, archive manifests, search FTS, and organization batches."""

    def _cancel_active_jobs(self, job_scope: str, document_ids: Sequence[int]) -> int:
        ids = sorted({int(document_id) for document_id in document_ids})
        if not ids:
            return 0

        cancelled = 0
        now = time.time()
        with self.cm.transaction(immediate=True) as conn:
            # Stay below SQLite's parameter limit while preserving one atomic
            # cancellation boundary for this scope and ID set.
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                cur = conn.execute(
                    "UPDATE document_index_jobs "
                    "SET status='cancelled', lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                    f"WHERE job_scope=? AND document_id IN ({placeholders}) "
                    "AND status IN ('queued','retry','running')",
                    (now, job_scope, *batch),
                )
                cancelled += cur.rowcount
        return cancelled

    def cancel_material_jobs_for_documents(self, document_ids: Sequence[int]) -> int:
        """Cancel active material jobs for these document IDs, clearing leases."""
        return self._cancel_active_jobs("material", document_ids)

    def cancel_wiki_reindex_jobs(self, page_ids: Sequence[int]) -> int:
        """Cancel active Wiki jobs for these page IDs, clearing leases."""
        return self._cancel_active_jobs("wiki", page_ids)

    def _enqueue_latest_index_job_in_tx(
        self,
        conn: sqlite3.Connection,
        document_id: int,
        version_id: int,
        content_hash: str,
        not_before: float,
    ) -> int:
        """Cancel prior active work and enqueue one latest-version job in the caller's transaction."""
        now = time.time()
        conn.execute(
            "UPDATE document_index_jobs SET status='cancelled', updated_at=? "
            "WHERE document_id=? AND job_scope='material' "
            "AND status IN ('queued','retry','running')",
            (now, document_id),
        )
        cur = conn.execute(
            "INSERT INTO document_index_jobs "
            "(document_id, version_id, content_hash, job_type, job_scope, status, "
            " available_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 'material_index', 'material', 'queued', ?, ?, ?)",
            (document_id, version_id, content_hash, not_before, now, now),
        )
        return int(cur.lastrowid)

    def enqueue_latest_index_job(
        self, document_id: int, version_id: int, content_hash: str, not_before: float
    ) -> int:
        """Enqueue the latest index job for a document, cancelling prior active ones."""
        with self.cm.transaction(immediate=True) as conn:
            return self._enqueue_latest_index_job_in_tx(
                conn, document_id, version_id, content_hash, not_before
            )

    def finalize_material_upload(
        self,
        document_id: int,
        version_id: int,
        staged_path: str | Path,
        final_path: str | Path,
        content_hash: str,
        not_before: float,
    ) -> int:
        """Commit a promoted path and its index job using one transaction."""
        staged = str(staged_path)
        final = str(final_path)
        with self.cm.transaction(immediate=True) as conn:
            permission = conn.execute(
                "SELECT kb_id FROM document_permissions WHERE id=?",
                (document_id,),
            ).fetchone()
            if permission is None:
                raise VersionConflict("document permission no longer exists")
            assert_kb_available_in_tx(conn, permission["kb_id"])

            version = conn.execute(
                "SELECT status, file_path, content_hash FROM document_versions "
                "WHERE id=? AND document_id=?",
                (version_id, document_id),
            ).fetchone()
            if (
                version is None
                or version["status"] != "staging"
                or version["file_path"] != staged
                or version["content_hash"] != content_hash
            ):
                raise VersionConflict("staging version changed before material finalization")

            updated = conn.execute(
                "UPDATE document_versions SET file_path=? "
                "WHERE id=? AND document_id=? AND status='staging' AND file_path=? "
                "AND content_hash=?",
                (final, version_id, document_id, staged, content_hash),
            )
            if updated.rowcount != 1:
                raise VersionConflict("staging version changed before material finalization")
            return self._enqueue_latest_index_job_in_tx(
                conn, document_id, version_id, content_hash, not_before
            )

    def enqueue_material_job(self, document_id: int, version_id: int) -> int:
        """Enqueue a job immediately (test and maintenance helper)."""
        now = time.time()
        with self.cm.transaction(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO document_index_jobs "
                "(document_id, version_id, content_hash, status, available_at, created_at, updated_at) "
                "VALUES (?, ?, '', 'queued', 0, ?, ?)",
                (document_id, version_id, now, now),
            )
            return int(cur.lastrowid)

    def get_index_job(self, job_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM document_index_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def settle_material_job(
        self,
        job_id: int,
        worker_id: str,
        status: str,
        error_code: str = "",
        available_at: float | None = None,
    ) -> bool:
        """Finalize a claimed material job and release its lease."""
        if status not in {"done", "failed", "cancelled", "retry"}:
            raise ValueError(f"Unsupported material job status: {status}")
        with self.cm.transaction(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE document_index_jobs "
                "SET status=?, error_code=?, available_at=COALESCE(?, available_at), "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=?",
                (status, error_code, available_at, time.time(), job_id, worker_id),
            )
        return cur.rowcount == 1

    def renew_material_job(
        self, job_id: int, worker_id: str, now: float, lease_seconds: int
    ) -> bool:
        """Extend the current worker's lease before a long job can be reclaimed."""
        with self.cm.transaction(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE document_index_jobs SET lease_expires_at=?, updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=?",
                (now + lease_seconds, now, job_id, worker_id),
            )
        return cur.rowcount == 1

    def claim_material_jobs(
        self,
        worker_id: str,
        now: float,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        """Claim due, unlocked jobs with one compare-and-set update each."""
        lease_expires = now + lease_seconds
        claimed: list[dict[str, Any]] = []
        with self.cm.transaction(immediate=True) as conn:
            candidates = conn.execute(
                "SELECT id FROM document_index_jobs "
                "WHERE status IN ('queued','retry','running') AND available_at <= ? "
                "AND (lease_expires_at IS NULL OR lease_expires_at < ?) "
                "ORDER BY available_at LIMIT ?",
                (now, now, limit),
            ).fetchall()
            for row in candidates:
                cur = conn.execute(
                    "UPDATE document_index_jobs "
                    "SET status='running', lease_owner=?, lease_expires_at=?, "
                    "attempt=attempt+1, updated_at=? "
                    "WHERE id=? AND available_at <= ? "
                    "AND (status IN ('queued','retry') "
                    "     OR (status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?))",
                    (worker_id, lease_expires, now, row["id"], now, now),
                )
                if cur.rowcount == 1:
                    job_row = conn.execute(
                        "SELECT * FROM document_index_jobs WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                    claimed.append(dict(job_row))
        return claimed

    def create_material_archive_manifest(
        self,
        container_document_id: int,
        container_version_id: int,
        source_path: str,
        created_by: int,
        expires_at: float,
    ) -> int:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO material_archive_manifests "
                "(container_document_id, container_version_id, source_path, status, created_by, expires_at) "
                "VALUES (?, ?, ?, 'pending_confirmation', ?, ?)",
                (container_document_id, container_version_id, source_path, created_by, expires_at),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_material_archive_manifest(self, manifest_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM material_archive_manifests WHERE id = ?", (manifest_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_material_archive_manifest_status(self, manifest_id: int, status: str) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE material_archive_manifests SET status = ? WHERE id = ?",
                (status, manifest_id),
            )
            conn.commit()

    def add_material_archive_entry(
        self,
        manifest_id: int,
        relative_path: str,
        normalized_path: str,
        child_source_key: str,
        size_bytes: int,
        state: str,
        reason_code: str = "",
    ) -> int:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO material_archive_entries "
                "(manifest_id, relative_path, normalized_path, child_source_key, size_bytes, content_hash, state, reason_code) "
                "VALUES (?, ?, ?, ?, ?, '', ?, ?)",
                (manifest_id, relative_path, normalized_path, child_source_key, size_bytes, state, reason_code),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_material_archive_entries(self, manifest_id: int) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM material_archive_entries WHERE manifest_id = ? ORDER BY id",
                (manifest_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_material_archive_entry_content(self, entry_id: int, content_hash: str) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE material_archive_entries SET content_hash = ? WHERE id = ?",
                (content_hash, entry_id),
            )
            conn.commit()

    def get_container_child(
        self, container_document_id: int, relative_path: str
    ) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, doc_name, kb_id, owner_id, current_version_id, "
                "container_document_id, container_relative_path "
                "FROM document_permissions "
                "WHERE container_document_id = ? AND container_relative_path = ?",
                (container_document_id, relative_path),
            ).fetchone()
        return dict(row) if row else None

    def set_child_container(
        self, document_id: int, container_document_id: int, relative_path: str
    ) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE document_permissions "
                "SET container_document_id = ?, container_relative_path = ? WHERE id = ?",
                (container_document_id, relative_path, document_id),
            )
            conn.commit()

    def list_container_children(self, container_document_id: int) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT dp.id AS document_id, dp.doc_name, dp.container_relative_path, "
                "dv.id AS version_id, dv.version_no, dv.content_hash "
                "FROM document_permissions dp "
                "JOIN document_versions dv ON dv.id = dp.current_version_id AND dv.status = 'current' "
                "WHERE dp.container_document_id = ? "
                "ORDER BY dp.container_relative_path",
                (container_document_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_archive_import_result(
        self, manifest_id: int, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM material_archive_import_results "
                "WHERE manifest_id = ? AND idempotency_key = ?",
                (manifest_id, idempotency_key),
            ).fetchone()
        return dict(row) if row else None

    def create_archive_import_result(
        self,
        manifest_id: int,
        idempotency_key: str,
        container_document_id: int,
        container_version_id: int,
        result_json: str,
    ) -> int:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO material_archive_import_results "
                "(manifest_id, idempotency_key, container_document_id, container_version_id, result_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (manifest_id, idempotency_key, container_document_id, container_version_id, result_json),
            )
            conn.commit()
            return int(cur.lastrowid)

    def insert_material_search_fts(
        self,
        document_id: int,
        version_id: int,
        filename: str,
        logical_path: str,
        document_type: str,
        tags: str,
        extracted_text: str,
    ) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "INSERT INTO material_search_fts "
                "(document_id, version_id, filename, logical_path, document_type, tags, extracted_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, version_id, filename, logical_path, document_type, tags, extracted_text),
            )
            conn.commit()

    def search_material_search_fts(self, text: str, limit: int = 50) -> list[dict[str, Any]]:
        match = _fts_phrase_query(text)
        if not match:
            return []
        with self.cm.read_connection() as conn:
            try:
                rows = conn.execute(
                    "SELECT f.document_id, f.version_id, f.filename, f.logical_path, f.document_type "
                    "FROM material_search_fts f "
                    "JOIN document_versions dv ON dv.id = f.version_id AND dv.status = 'current' "
                    "JOIN document_permissions dp ON dp.id = f.document_id AND dp.deleted_at IS NULL "
                    "WHERE material_search_fts MATCH ? LIMIT ?",
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if not _is_fts_syntax_error(exc):
                    # A locked/corrupt/missing schema is not "no results".
                    logger.error(
                        "material FTS query failed: %s", exc, extra={"metric": "material_fts_error"}
                    )
                    raise
                # Escaping above makes this reachable only for exotic tokens;
                # keep the "invalid query means no hits" contract but make it
                # observable instead of silently indistinguishable from a miss.
                logger.warning(
                    "material FTS query rejected as invalid syntax",
                    extra={"metric": "material_fts_syntax_error"},
                )
                return []
        return [dict(r) for r in rows]

    def list_material_documents_with_current_version(self) -> list[dict[str, Any]]:
        """Load live material documents together with their current version.

        Material location and organization used to call
        ``get_document_permission_by_id`` and ``get_current_document_version``
        once per document (D observation: locator/organizer N+1).  This single
        query returns the same row shape for every live document, so callers can
        then resolve authorization once per knowledge base.
        """
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT dp.id, dp.doc_name, dp.kb_id, dp.owner_id, dp.scope, "
                "dp.protected, dp.current_version_id, dp.logical_path, "
                "dp.document_type, dp.tags_json, dp.subject_user_id, dp.updated_at, "
                "dp.metadata_version, dp.deleted_at, "
                "dv.original_filename AS current_filename, "
                "dv.validity_status AS current_validity_status, "
                "dv.version_no AS current_version_no "
                "FROM document_permissions dp "
                "JOIN document_versions dv "
                "ON dv.id = dp.current_version_id AND dv.status = 'current' "
                "WHERE dp.deleted_at IS NULL "
                "ORDER BY dp.id"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_organization_proposal(
        self, actor_id: int, instruction_hash: str, expires_at: float
    ) -> int:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO material_organization_proposals "
                "(actor_id, instruction_hash, status, expires_at, created_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (actor_id, instruction_hash, expires_at, time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_organization_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM material_organization_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_organization_proposal_status(self, proposal_id: int, status: str) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE material_organization_proposals SET status = ? WHERE id = ?",
                (status, proposal_id),
            )
            conn.commit()

    def add_organization_item(
        self,
        proposal_id: int,
        document_id: int,
        target_path: str,
        add_tags: str,
        remove_tags: str,
        document_type: str,
        reason: str,
        confidence: float,
    ) -> int:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO material_organization_items "
                "(proposal_id, document_id, target_path, add_tags, remove_tags, document_type, reason, confidence, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (proposal_id, document_id, target_path, add_tags, remove_tags, document_type, reason, confidence),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_organization_items(self, proposal_id: int) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM material_organization_items WHERE proposal_id = ? ORDER BY id",
                (proposal_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_organization_item_status(
        self, item_id: int, status: str, reason_code: str = ""
    ) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE material_organization_items SET status = ?, reason_code = ? WHERE id = ?",
                (status, reason_code, item_id),
            )
            conn.commit()

    def get_organization_batch(
        self, proposal_id: int, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM material_organization_batches "
                "WHERE proposal_id = ? AND idempotency_key = ?",
                (proposal_id, idempotency_key),
            ).fetchone()
        return dict(row) if row else None

    def _create_organization_batch_in_tx(
        self, proposal_id: int, actor_id: int, idempotency_key: str
    ) -> int:
        """Insert a batch row inside an already-open transaction."""
        conn = self.cm.get_write_connection()
        cur = conn.execute(
            "INSERT INTO material_organization_batches "
            "(proposal_id, actor_id, status, created_at, idempotency_key) "
            "VALUES (?, ?, 'applied', ?, ?)",
            (proposal_id, actor_id, time.time(), idempotency_key),
        )
        return int(cur.lastrowid)

    def create_organization_batch(
        self, proposal_id: int, actor_id: int, idempotency_key: str
    ) -> int:
        with self.cm.write_connection():
            return self._create_organization_batch_in_tx(proposal_id, actor_id, idempotency_key)

    def add_organization_batch_item(
        self,
        batch_id: int,
        document_id: int,
        before_logical_path: str,
        after_logical_path: str,
        before_metadata_version: int,
        after_metadata_version: int,
        status: str,
        reason_code: str,
        *,
        before_tags_json: str | None = None,
        after_tags_json: str | None = None,
        before_document_type: str | None = None,
        after_document_type: str | None = None,
    ) -> int:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO material_organization_batch_items "
                "(batch_id, document_id, before_logical_path, after_logical_path, "
                "before_metadata_version, after_metadata_version, status, reason_code, "
                "before_tags_json, after_tags_json, before_document_type, after_document_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id, document_id, before_logical_path, after_logical_path,
                    before_metadata_version, after_metadata_version, status, reason_code,
                    before_tags_json, after_tags_json,
                    before_document_type, after_document_type,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_organization_batch_items(self, batch_id: int) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM material_organization_batch_items WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_organization_batch_by_id(self, batch_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM material_organization_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None

    def store_unit_fingerprints(
        self, document_id: int, version_id: int, fingerprints: dict[str, str]
    ) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "DELETE FROM material_unit_fingerprints WHERE version_id = ?",
                (version_id,),
            )
            conn.executemany(
                "INSERT INTO material_unit_fingerprints "
                "(document_id, version_id, unit_key, digest) VALUES (?, ?, ?, ?)",
                [(document_id, version_id, key, digest) for key, digest in fingerprints.items()],
            )
            conn.commit()

    def load_unit_fingerprints(self, document_id: int, version_id: int) -> dict[str, str]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT unit_key, digest FROM material_unit_fingerprints WHERE version_id = ?",
                (version_id,),
            ).fetchall()
        return {r["unit_key"]: r["digest"] for r in rows}
