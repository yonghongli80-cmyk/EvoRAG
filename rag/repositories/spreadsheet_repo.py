"""Structured datasets and spreadsheet edit sessions repository."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from rag.repositories.base import BaseRepository, VersionConflict, canonical_json_text


class SpreadsheetRepository(BaseRepository):
    """Handles structured datasets, spreadsheet edits, audit logs, and sessions."""

    def create_structured_dataset(
        self, document_id: int, document_version_id: int, fmt: str, semantic_index_mode: str
    ) -> int:
        """Reserve one staging dataset bound to one immutable document version."""
        with self.cm.transaction(immediate=True) as conn:
            version = conn.execute(
                "SELECT document_id FROM document_versions WHERE id = ?", (document_version_id,)
            ).fetchone()
            if version is None or int(version["document_id"]) != document_id:
                raise ValueError("document version does not belong to document")
            storage_key = f"{document_id}/{document_version_id}/dataset.sqlite3"
            cur = conn.execute(
                "INSERT INTO structured_datasets "
                "(document_id, document_version_id, format, status, storage_key, semantic_index_mode) "
                "VALUES (?, ?, ?, 'staging', ?, ?)",
                (document_id, document_version_id, fmt, storage_key, semantic_index_mode),
            )
            return int(cur.lastrowid)

    def activate_structured_dataset(self, dataset_id: int) -> None:
        """Mark a fully validated staging dataset ready without changing document state."""
        with self.cm.transaction(immediate=True) as conn:
            updated = conn.execute(
                "UPDATE structured_datasets SET status = 'ready', ready_at = ? "
                "WHERE id = ? AND status = 'staging'",
                (time.time(), dataset_id),
            )
            if updated.rowcount != 1:
                raise VersionConflict("dataset is no longer staging")

    def _activate_structured_dataset_in_tx(self, document_id: int, dataset_id: int) -> None:
        """Promote a staging dataset while its caller owns the SQLite transaction."""
        conn = self.cm.get_write_connection()
        updated = conn.execute(
            "UPDATE structured_datasets SET status = 'ready', ready_at = ? "
            "WHERE id = ? AND document_id = ? AND status = 'staging'",
            (time.time(), dataset_id, document_id),
        )
        if updated.rowcount != 1:
            raise VersionConflict("dataset is no longer staging")

    def activate_document_and_dataset(
        self,
        *,
        document_id: int,
        version_id: int,
        dataset_id: int,
        kb_id: str,
        filename: str,
    ) -> None:
        """Atomically make a document version and its dataset current and ready."""
        with self.cm.transaction(immediate=True) as conn:
            dataset = conn.execute(
                "SELECT document_version_id FROM structured_datasets "
                "WHERE id = ? AND document_id = ?",
                (dataset_id, document_id),
            ).fetchone()
            if dataset is None or int(dataset["document_version_id"]) != version_id:
                raise ValueError("dataset does not belong to document version")
            if self.db is not None:
                self.db._activate_document_version_in_tx(document_id, version_id, kb_id, filename)
            self._activate_structured_dataset_in_tx(document_id, dataset_id)

    def mark_structured_dataset_failed(self, dataset_id: int, error_code: str) -> None:
        """Record an extraction failure without altering any current-version pointer."""
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE structured_datasets SET status = 'failed', last_error_code = ? "
                "WHERE id = ? AND status = 'staging'",
                (error_code, dataset_id),
            )
            conn.commit()

    def enqueue_vector_flag_repair(self, document_id: int, version_id: int) -> None:
        """Persist idempotent cleanup work after the authoritative SQLite switch succeeds."""
        with self.cm.write_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO vector_flag_repairs (document_id, version_id) VALUES (?, ?)",
                (document_id, version_id),
            )
            conn.commit()

    def list_vector_flag_repairs(self) -> list[dict[str, int]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT document_id, version_id FROM vector_flag_repairs "
                "ORDER BY document_id, version_id"
            ).fetchall()
        return [{"document_id": int(row["document_id"]), "version_id": int(row["version_id"])} for row in rows]

    def get_structured_dataset_for_version(self, version_id: int) -> dict[str, Any] | None:
        """Return metadata for exactly one document version, never a current-version fallback."""
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, document_id, document_version_id, format, status, storage_key, "
                "content_hash, sheet_count, total_rows, total_columns, semantic_index_mode, "
                "extraction_version, created_at, ready_at, last_error_code "
                "FROM structured_datasets WHERE document_version_id = ?",
                (version_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_current_ready_spreadsheet_datasets(self, kb_id: str) -> list[dict[str, Any]]:
        """Return ready datasets attached to current documents in one knowledge base."""
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT sd.id, sd.document_id, sd.document_version_id, sd.format, "
                "sd.storage_key, sd.content_hash, sd.sheet_count, sd.total_rows, "
                "sd.total_columns, sd.ready_at, dp.doc_name, dp.kb_id, dp.owner_id, dp.scope "
                "FROM structured_datasets sd "
                "JOIN document_versions dv ON dv.id = sd.document_version_id "
                "JOIN document_permissions dp ON dp.id = sd.document_id "
                "WHERE dp.kb_id = ? AND sd.status = 'ready' AND dv.status = 'current' "
                "AND dv.validity_status = 'valid' "
                "ORDER BY dp.doc_name, sd.id",
                (kb_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_structured_dataset_schema(self, dataset_id: int) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT ss.sheet_key, ss.name AS sheet_name, ss.ordinal AS sheet_ordinal, "
                "ss.row_count, ss.column_count, sc.column_key, sc.ordinal AS column_ordinal, "
                "sc.display_name, sc.normalized_name, sc.inferred_type, sc.number_format, "
                "sc.unit, sc.sample_values_json "
                "FROM structured_sheets ss JOIN structured_columns sc ON sc.sheet_id = ss.id "
                "WHERE ss.dataset_id = ? ORDER BY ss.ordinal, sc.ordinal",
                (dataset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_structured_dataset_schema(
        self, dataset_id: int, sheets: Sequence[Mapping[str, Any]]
    ) -> None:
        """Rewrite one dataset's mirrored sheet/column manifest atomically.

        The dataset file stays the source of truth, so the whole mirror is
        replaced inside one transaction (delete then insert) to keep repeated
        calls idempotent.  ``sheets`` is an ordered sequence of mappings with
        ``sheet_key``, ``name``, ``ordinal``, ``visibility``, ``header_row``,
        ``first_data_row``, ``row_count``, ``column_count``,
        ``merged_range_count``, ``has_formulas`` and ``columns``; each column
        mapping carries ``column_key``, ``ordinal``, ``display_name``,
        ``normalized_name``, ``inferred_type``, ``number_format``, ``unit`` and
        ``sample_values_json``.

        The dataset's ``sheet_count``/``total_rows``/``total_columns`` registry
        counters are rewritten from the same payload in the same transaction.
        """
        with self.cm.transaction(immediate=True) as conn:
            conn.execute(
                "DELETE FROM structured_columns WHERE sheet_id IN "
                "(SELECT id FROM structured_sheets WHERE dataset_id = ?)",
                (dataset_id,),
            )
            conn.execute(
                "DELETE FROM structured_sheets WHERE dataset_id = ?", (dataset_id,)
            )
            sheet_count = 0
            total_rows = 0
            total_columns = 0
            for sheet in sheets:
                cursor = conn.execute(
                    "INSERT INTO structured_sheets "
                    "(dataset_id, sheet_key, name, ordinal, visibility, header_row, "
                    "first_data_row, row_count, column_count, merged_range_count, "
                    "has_formulas) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        dataset_id,
                        sheet["sheet_key"],
                        sheet["name"],
                        int(sheet["ordinal"]),
                        sheet["visibility"],
                        sheet.get("header_row"),
                        sheet.get("first_data_row"),
                        int(sheet.get("row_count", 0)),
                        int(sheet.get("column_count", 0)),
                        int(sheet.get("merged_range_count", 0)),
                        int(bool(sheet.get("has_formulas", False))),
                    ),
                )
                sheet_id = int(cursor.lastrowid)
                columns = sheet.get("columns", ())
                conn.executemany(
                    "INSERT INTO structured_columns "
                    "(sheet_id, column_key, ordinal, display_name, normalized_name, "
                    "inferred_type, number_format, unit, sample_values_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            sheet_id,
                            column["column_key"],
                            int(column["ordinal"]),
                            column["display_name"],
                            column["normalized_name"],
                            column["inferred_type"],
                            column.get("number_format", ""),
                            column.get("unit", ""),
                            column.get("sample_values_json", "[]"),
                        )
                        for column in columns
                    ],
                )
                sheet_count += 1
                total_rows += int(sheet.get("row_count", 0))
                total_columns += len(columns)
            conn.execute(
                # The material worker records these three counters itself; the
                # migration and backfill paths only write this mirror, so the
                # registry has to derive them from the mirror in the same
                # transaction instead of leaving them at zero.
                "UPDATE structured_datasets SET sheet_count = ?, total_rows = ?, "
                "total_columns = ? WHERE id = ?",
                (sheet_count, total_rows, total_columns, dataset_id),
            )

    def record_structured_query_run(
        self,
        user_id: int,
        dataset_ids: list[int],
        plan: dict[str, Any],
        status: str,
        error_code: str | None = None,
        rows_scanned: int = 0,
        duration_ms: float = 0.0,
    ) -> int:
        plan_text = json.dumps(plan or {}, ensure_ascii=False, sort_keys=True, default=str)
        with self.cm.write_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO structured_query_runs (user_id,dataset_version_ids,plan_json,used_columns_json,filter_summary_json,rows_scanned,rows_returned,status,error_code,duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (user_id, json.dumps(list(dataset_ids)), plan_text, "[]", "[]", rows_scanned, 0, status, error_code, duration_ms),
            )
            conn.commit()
            return int(cursor.lastrowid)

    @staticmethod
    def _canonical_json_text(value: Any) -> str:
        """Return deterministic JSON for a value or an existing JSON string."""
        return canonical_json_text(json.loads(value) if isinstance(value, str) else value)

    def reserve_spreadsheet_edit(
        self,
        *,
        document_id: int,
        base_version_id: int,
        actor_user_id: int,
        original_filename: str,
        staged_path: str,
        content_hash: str,
        operation: str,
        reason: str,
        restored_from_version_id: int | None,
        changes: tuple[dict[str, Any], ...],
        ip_address: str = "",
        user_agent: str = "",
        risk_flags: tuple[str, ...] = (),
        structure_change: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        """Atomically reserve a spreadsheet version and its immutable audit rows."""
        if operation not in {"edit", "rollback"}:
            raise ValueError("invalid spreadsheet edit operation")
        with self.cm.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT dp.current_version_id, dv.status "
                "FROM document_permissions dp "
                "LEFT JOIN document_versions dv ON dv.id = dp.current_version_id "
                "WHERE dp.id = ?",
                (document_id,),
            ).fetchone()
            if (
                current is None
                or current["current_version_id"] != base_version_id
                or current["status"] != "current"
            ):
                raise VersionConflict("spreadsheet_base_version_conflict")

            if restored_from_version_id is not None:
                source = conn.execute(
                    "SELECT document_id FROM document_versions WHERE id = ?",
                    (restored_from_version_id,),
                ).fetchone()
                if source is None or source["document_id"] != document_id:
                    raise VersionConflict("spreadsheet_restore_source_conflict")

            next_row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no "
                "FROM document_versions WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            version_cur = conn.execute(
                "INSERT INTO document_versions "
                "(document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, restored_from_version_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'staging', ?)",
                (
                    document_id,
                    int(next_row["next_no"]),
                    original_filename,
                    staged_path,
                    content_hash,
                    actor_user_id,
                    restored_from_version_id,
                ),
            )
            version_id = int(version_cur.lastrowid)
            session_cur = conn.execute(
                "INSERT INTO spreadsheet_edit_sessions "
                "(document_id, base_version_id, new_version_id, actor_user_id, operation, "
                "status, change_count, restored_from_version_id, document_name_snapshot, "
                "reason, ip_address, user_agent, risk_flags_json) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    base_version_id,
                    version_id,
                    actor_user_id,
                    operation,
                    1 if structure_change is not None else len(changes),
                    restored_from_version_id,
                    original_filename,
                    reason,
                    ip_address,
                    user_agent,
                    json.dumps(
                        sorted(set(risk_flags)),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )
            session_id = int(session_cur.lastrowid)
            conn.executemany(
                "INSERT INTO spreadsheet_cell_changes "
                "(session_id, sheet_key, sheet_name_snapshot, cell_address, "
                "old_value_type, old_value_json, new_value_type, new_value_json, sensitive) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        session_id,
                        change["sheet_key"],
                        change["sheet_name_snapshot"],
                        change["cell_address"],
                        change["old_value_type"],
                        self._canonical_json_text(change["old_value_json"]),
                        change["new_value_type"],
                        self._canonical_json_text(change["new_value_json"]),
                        int(bool(change.get("sensitive", 0))),
                    )
                    for change in changes
                ],
            )
            if structure_change is not None:
                conn.execute(
                    "INSERT INTO spreadsheet_structure_changes "
                    "(session_id, action, sheet_key, sheet_name_snapshot, target_index, "
                    "count, new_sheet_name, affected_nonempty_cells, confirm_nonempty) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        structure_change["action"],
                        structure_change.get("sheet_key"),
                        structure_change.get("sheet_name_snapshot"),
                        structure_change.get("target_index"),
                        structure_change.get("count", 1),
                        structure_change.get("new_sheet_name"),
                        structure_change.get("affected_nonempty_cells", 0),
                        int(bool(structure_change.get("confirm_nonempty", 0))),
                    ),
                )
            return session_id, version_id

    def reserve_spreadsheet_structure_edit(
        self,
        *,
        document_id: int,
        base_version_id: int,
        actor_user_id: int,
        original_filename: str,
        staged_path: str,
        content_hash: str,
        reason: str,
        structure_change: dict[str, Any],
        ip_address: str = "",
        user_agent: str = "",
        risk_flags: tuple[str, ...] = (),
    ) -> tuple[int, int]:
        """Atomically reserve an immutable version and one structure audit row."""
        return self.reserve_spreadsheet_edit(
            document_id=document_id,
            base_version_id=base_version_id,
            actor_user_id=actor_user_id,
            original_filename=original_filename,
            staged_path=staged_path,
            content_hash=content_hash,
            operation="edit",
            reason=reason,
            restored_from_version_id=None,
            changes=(),
            ip_address=ip_address,
            user_agent=user_agent,
            risk_flags=risk_flags,
            structure_change=structure_change,
        )

    def count_recent_spreadsheet_edits(
        self, actor_user_id: int, document_id: int, since: float
    ) -> int:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM spreadsheet_edit_sessions "
                "WHERE actor_user_id=? AND document_id=? AND created_at>=?",
                (actor_user_id, document_id, since),
            ).fetchone()
        return int(row[0])

    def record_rejected_spreadsheet_edit(
        self,
        *,
        document_id: int,
        base_version_id: int,
        actor_user_id: int,
        operation: str,
        document_name_snapshot: str,
        reason: str,
        error_code: str,
        risk_flags: tuple[str, ...],
        ip_address: str = "",
        user_agent: str = "",
    ) -> int:
        """Persist a value-free audit record for a server-rejected edit attempt."""
        with self.cm.transaction(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO spreadsheet_edit_sessions "
                "(document_id, base_version_id, new_version_id, actor_user_id, operation, "
                "status, change_count, document_name_snapshot, reason, ip_address, "
                "user_agent, risk_flags_json, error_code) "
                "VALUES (?, ?, NULL, ?, ?, 'rejected', 0, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    base_version_id,
                    actor_user_id,
                    operation,
                    document_name_snapshot,
                    reason,
                    ip_address,
                    user_agent,
                    json.dumps(sorted(set(risk_flags)), separators=(",", ":")),
                    error_code,
                ),
            )
            return int(cur.lastrowid)

    def get_spreadsheet_edit_session(self, session_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM spreadsheet_edit_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_spreadsheet_edit_session_for_version(
        self, version_id: int
    ) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM spreadsheet_edit_sessions WHERE new_version_id = ?",
                (version_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_spreadsheet_edit_status(
        self,
        session_id: int,
        status: str,
        *,
        activated_at: float | None = None,
        error_code: str = "",
    ) -> None:
        allowed = {
            "pending": {"active", "failed", "cancelled"},
            "active": set(),
            "failed": set(),
            "cancelled": set(),
            "rejected": set(),
        }
        if status not in allowed:
            raise ValueError("invalid spreadsheet edit status")
        with self.cm.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT status FROM spreadsheet_edit_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None or status not in allowed[row["status"]]:
                raise VersionConflict("spreadsheet_edit_status_conflict")
            active_time = (
                activated_at if activated_at is not None else time.time()
            ) if status == "active" else None
            conn.execute(
                "UPDATE spreadsheet_edit_sessions "
                "SET status = ?, activated_at = ?, error_code = ? WHERE id = ?",
                (status, active_time, error_code, session_id),
            )
