"""Transaction-only persistence helpers for controlled staging recovery."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


class RecoveryReceiptConflict(RuntimeError):
    """The receipt key or metadata compare-and-swap no longer matches."""


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError("staging recovery repository requires caller transaction")


def get_recovery_receipt(conn: sqlite3.Connection, operation_key: str) -> dict[str, Any] | None:
    _require_transaction(conn)
    row = conn.execute(
        "SELECT operation_key, request_hash, actor_user_id, document_id, version_id, "
        "receipt_json, created_at FROM material_recovery_receipts WHERE operation_key=?",
        (operation_key,),
    ).fetchone()
    if row is None:
        return None
    result = (
        dict(row)
        if isinstance(row, sqlite3.Row)
        else {
            key: row[index]
            for index, key in enumerate(
                (
                    "operation_key",
                    "request_hash",
                    "actor_user_id",
                    "document_id",
                    "version_id",
                    "receipt_json",
                    "created_at",
                )
            )
        }
    )
    try:
        result["receipt"] = json.loads(result["receipt_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RecoveryReceiptConflict("stored recovery receipt is invalid") from exc
    return result


def insert_recovery_receipt(
    conn: sqlite3.Connection,
    *,
    operation_key: str,
    request_hash: str,
    actor_user_id: int,
    document_id: int,
    version_id: int,
    receipt: dict[str, Any],
) -> None:
    _require_transaction(conn)
    receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    try:
        conn.execute(
            "INSERT INTO material_recovery_receipts "
            "(operation_key, request_hash, actor_user_id, document_id, version_id, receipt_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                operation_key,
                request_hash,
                actor_user_id,
                document_id,
                version_id,
                receipt_json,
            ),
        )
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        if message in {
            "UNIQUE constraint failed: material_recovery_receipts.operation_key",
            "PRIMARY KEY constraint failed: material_recovery_receipts.operation_key",
        }:
            raise RecoveryReceiptConflict("recovery operation key already exists") from exc
        raise


def cas_recovery_paths(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    expected_version_path: str,
    destination_path: str,
    manifest_id: int | None = None,
    expected_manifest_source: str | None = None,
    update_version: bool = True,
) -> None:
    _require_transaction(conn)
    if (manifest_id is None) != (expected_manifest_source is None):
        raise RecoveryReceiptConflict("manifest compare-and-swap arguments are incomplete")

    version = conn.execute("SELECT file_path, status FROM document_versions WHERE id=?", (version_id,)).fetchone()
    if version is None or version[1] != "staging" or version[0] != expected_version_path:
        raise RecoveryReceiptConflict("document version changed before recovery")

    if manifest_id is not None:
        manifest = conn.execute(
            "SELECT source_path, status FROM material_archive_manifests WHERE id=?",
            (manifest_id,),
        ).fetchone()
        if manifest is None or manifest[1] != "pending_confirmation" or manifest[0] != expected_manifest_source:
            raise RecoveryReceiptConflict("archive manifest changed before recovery")

    if update_version:
        changed = conn.execute(
            "UPDATE document_versions SET file_path=? WHERE id=? AND status='staging' AND file_path=?",
            (destination_path, version_id, expected_version_path),
        ).rowcount
        if changed != 1:
            raise RecoveryReceiptConflict("document version compare-and-swap failed")

    if manifest_id is not None:
        changed = conn.execute(
            "UPDATE material_archive_manifests SET source_path=? "
            "WHERE id=? AND status='pending_confirmation' AND source_path=?",
            (destination_path, manifest_id, expected_manifest_source),
        ).rowcount
        if changed != 1:
            raise RecoveryReceiptConflict("archive manifest compare-and-swap failed")
