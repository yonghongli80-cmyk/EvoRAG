"""Permission-safe spreadsheet change auditing and deterministic risk rules."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any


def compute_risk_flags(
    operation: str,
    changes: Iterable[Mapping[str, object]],
    actor_history: int,
    document: Mapping[str, object],
) -> tuple[str, ...]:
    """Return stable, explainable flags without inspecting client-supplied labels."""
    change_rows = tuple(changes)
    flags: set[str] = set()
    if operation == "edit" and document.get("scope") == "public":
        flags.add("public_document_edit")
    if len(change_rows) > 100:
        flags.add("bulk_change")
    cleared = sum(
        1
        for change in change_rows
        if _json_nonempty(change.get("old_value_json"))
        and not _json_nonempty(change.get("new_value_json"))
    )
    if cleared > 20:
        flags.add("mass_clear")
    if actor_history > 5:
        flags.add("rapid_repeated_saves")
    if operation == "rollback" and document.get("non_previous_restore"):
        flags.add("non_previous_restore")
    rejected_reason = str(document.get("rejected_reason") or "")
    if rejected_reason in {
        "formula_edit_forbidden",
        "formula_cell_not_editable",
        "formula_injection",
    }:
        flags.add("rejected_formula_edit")
    if rejected_reason in {"protected_cell", "protected_cell_edit_forbidden"}:
        flags.add("rejected_protected_cell_edit")
    return tuple(sorted(flags))


def _json_nonempty(value: object) -> bool:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = value
    return decoded not in (None, "")


def list_spreadsheet_audits(
    db,
    user: Mapping[str, Any],
    *,
    limit: int,
    cursor: str = "",
    document_id: int | None = None,
    actor_user_id: int | None = None,
    kb_id: str = "",
    operation: str = "",
    status: str = "",
    risk: str = "",
    created_after: float | None = None,
    created_before: float | None = None,
) -> dict[str, object]:
    conditions = []
    params: list[object] = []
    if not user.get("is_admin"):
        conditions.append("dp.owner_id = ?")
        params.append(user["id"])
    for sql, value in (
        ("s.document_id = ?", document_id),
        ("s.actor_user_id = ?", actor_user_id),
        ("dp.kb_id = ?", kb_id or None),
        ("s.operation = ?", operation or None),
        ("s.status = ?", status or None),
        ("s.created_at >= ?", created_after),
        ("s.created_at <= ?", created_before),
    ):
        if value is not None:
            conditions.append(sql)
            params.append(value)
    if risk:
        conditions.append("s.risk_flags_json LIKE ?")
        params.append(f'%"{risk}"%')
    if cursor:
        try:
            cursor_time, cursor_id = cursor.split(":", 1)
            conditions.append("(s.created_at < ? OR (s.created_at = ? AND s.id < ?))")
            params.extend((float(cursor_time), float(cursor_time), int(cursor_id)))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid audit cursor") from exc
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with db._lock:
        if not user.get("is_admin"):
            owns = db._conn.execute(
                "SELECT 1 FROM document_permissions WHERE owner_id=? LIMIT 1",
                (user["id"],),
            ).fetchone()
            if owns is None:
                raise PermissionError("audit management permission required")
        rows = db._conn.execute(
            "SELECT s.id, s.document_id, s.base_version_id, s.new_version_id, "
            "s.actor_user_id, u.username AS actor_name, s.operation, s.status, "
            "s.change_count, s.restored_from_version_id, s.document_name_snapshot, "
            "dp.kb_id, s.reason, s.risk_flags_json, s.created_at, s.activated_at, "
            "s.error_code, sc.action AS structure_action "
            "FROM spreadsheet_edit_sessions s "
            "JOIN document_permissions dp ON dp.id=s.document_id "
            "JOIN users u ON u.id=s.actor_user_id "
            "LEFT JOIN spreadsheet_structure_changes sc ON sc.session_id=s.id "
            f"{where} ORDER BY s.created_at DESC, s.id DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    items = [_audit_summary(dict(row)) for row in page]
    next_cursor = ""
    if has_more and page:
        next_cursor = f'{page[-1]["created_at"]}:{page[-1]["id"]}'
    return {"items": items, "next_cursor": next_cursor}


def get_spreadsheet_audit_detail(db, user: Mapping[str, Any], session_id: int):
    with db._lock:
        row = db._conn.execute(
            "SELECT s.*, u.username AS actor_name, dp.owner_id, dp.kb_id "
            "FROM spreadsheet_edit_sessions s "
            "JOIN users u ON u.id=s.actor_user_id "
            "JOIN document_permissions dp ON dp.id=s.document_id WHERE s.id=?",
            (session_id,),
        ).fetchone()
        if row is None or not (
            user.get("is_admin") or row["owner_id"] == user.get("id")
        ):
            return None
        changes = db._conn.execute(
            "SELECT sheet_key, sheet_name_snapshot, cell_address, old_value_type, "
            "old_value_json, new_value_type, new_value_json, sensitive "
            "FROM spreadsheet_cell_changes WHERE session_id=? "
            "ORDER BY sheet_key, cell_address",
            (session_id,),
        ).fetchall()
        structure = db._conn.execute(
            "SELECT action, sheet_key, sheet_name_snapshot, target_index, count, "
            "new_sheet_name, affected_nonempty_cells, confirm_nonempty "
            "FROM spreadsheet_structure_changes WHERE session_id=?",
            (session_id,),
        ).fetchone()
    summary = _audit_summary(dict(row))
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for change in changes:
        key = (change["sheet_key"], change["sheet_name_snapshot"])
        item: dict[str, object] = {
            "cell": change["cell_address"],
            "old_value_type": change["old_value_type"],
            "new_value_type": change["new_value_type"],
        }
        if change["sensitive"]:
            item["redacted"] = True
        else:
            item["old_value"] = json.loads(change["old_value_json"])
            item["new_value"] = json.loads(change["new_value_json"])
        grouped.setdefault(key, []).append(item)
    summary["sheets"] = [
        {"sheet_key": key[0], "sheet_name": key[1], "changes": values}
        for key, values in grouped.items()
    ]
    summary["structure_change"] = (
        {
            "action": structure["action"],
            "sheet_key": structure["sheet_key"],
            "sheet_name": structure["sheet_name_snapshot"],
            "index": structure["target_index"],
            "count": structure["count"],
            "new_sheet_name": structure["new_sheet_name"],
            "affected_nonempty_cells": structure["affected_nonempty_cells"],
            "confirm_nonempty": bool(structure["confirm_nonempty"]),
        }
        if structure is not None
        else None
    )
    return summary


def _audit_summary(row: dict[str, Any]) -> dict[str, object]:
    return {
        "session_id": row["id"],
        "document_id": row["document_id"],
        "document_name": row["document_name_snapshot"],
        "kb_id": row["kb_id"],
        "base_version_id": row["base_version_id"],
        "new_version_id": row["new_version_id"],
        "actor_user_id": row["actor_user_id"],
        "actor_name": row["actor_name"],
        "operation": row["operation"],
        "status": row["status"],
        "change_count": row["change_count"],
        "restored_from_version_id": row["restored_from_version_id"],
        "reason": row["reason"],
        "risk_flags": json.loads(row["risk_flags_json"]),
        "created_at": row["created_at"],
        "activated_at": row["activated_at"],
        "error_code": row["error_code"],
        "structure_action": row.get("structure_action"),
    }
