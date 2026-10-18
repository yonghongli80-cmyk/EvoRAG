"""Material index jobs, archive manifests, FTS, and organization proposals repository."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from rag.repositories.base import BaseRepository


class MaterialRepository(BaseRepository):
    """Handles index jobs, archive manifests, search FTS, and organization batches."""

    def enqueue_latest_index_job(
        self, document_id: int, version_id: int, content_hash: str, not_before: float
    ) -> int:
        """Enqueue the latest index job for a document, cancelling prior active ones."""
        now = time.time()
        with self.cm.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE document_index_jobs SET status='cancelled', updated_at=? "
                "WHERE document_id=? AND status IN ('queued','retry','running')",
                (now, document_id),
            )
            cur = conn.execute(
                "INSERT INTO document_index_jobs "
                "(document_id, version_id, content_hash, status, available_at, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (document_id, version_id, content_hash, not_before, now, now),
            )
            return int(cur.lastrowid)

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
        with self.cm.read_connection() as conn:
            try:
                rows = conn.execute(
                    "SELECT f.document_id, f.version_id, f.filename, f.logical_path, f.document_type "
                    "FROM material_search_fts f "
                    "JOIN document_versions dv ON dv.id = f.version_id AND dv.status = 'current' "
                    "JOIN document_permissions dp ON dp.id = f.document_id AND dp.deleted_at IS NULL "
                    "WHERE material_search_fts MATCH ? LIMIT ?",
                    (text, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

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
    ) -> int:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO material_organization_batch_items "
                "(batch_id, document_id, before_logical_path, after_logical_path, "
                "before_metadata_version, after_metadata_version, status, reason_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id, document_id, before_logical_path, after_logical_path,
                    before_metadata_version, after_metadata_version, status, reason_code,
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
