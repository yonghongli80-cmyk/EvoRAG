"""Deterministic knowledge-task rules, state transitions, and reassignment."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from config import settings
from rag.user_db import UserDB, VersionConflict

_ALLOWED_TRANSITIONS = {
    "open": {"in_progress", "resolved", "dismissed", "confirmed_expired"},
    "in_progress": {"resolved", "dismissed", "confirmed_expired"},
    "resolved": set(),
    "dismissed": set(),
    "confirmed_expired": set(),
}

TASK_TYPES = ("outdated", "not_useful", "insufficient_evidence", "evidence_conflict")
_TASK_COLUMNS = (
    "id", "task_type", "cluster_key", "active_key", "document_id",
    "document_version_id", "assigned_to", "status", "report_count",
    "resolution_note", "due_at", "created_at", "updated_at", "resolved_at",
)
_TASKS_TABLE_SQL = """
CREATE TABLE knowledge_tasks_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL CHECK(task_type IN (
        'outdated', 'not_useful', 'insufficient_evidence', 'evidence_conflict'
    )),
    cluster_key TEXT NOT NULL,
    active_key TEXT UNIQUE,
    document_id INTEGER,
    document_version_id INTEGER,
    assigned_to INTEGER,
    status TEXT NOT NULL CHECK(status IN (
        'open', 'in_progress', 'resolved', 'dismissed', 'confirmed_expired'
    )),
    report_count INTEGER NOT NULL DEFAULT 1,
    resolution_note TEXT NOT NULL DEFAULT '',
    due_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL
)
"""


def migrate_knowledge_task_types(connection: sqlite3.Connection) -> bool:
    """Widen a legacy two-type task table without losing its schema objects."""
    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_tasks'"
    ).fetchone()
    if table is None:
        return False
    table_sql = table[0] or ""
    if all(task_type in table_sql for task_type in TASK_TYPES):
        return False

    source_columns = tuple(
        row[1] for row in connection.execute("PRAGMA table_info(knowledge_tasks)")
    )
    if source_columns != _TASK_COLUMNS:
        raise RuntimeError("unsupported knowledge_tasks schema")
    schema_objects = connection.execute(
        """
        SELECT type, sql FROM sqlite_master
        WHERE tbl_name = 'knowledge_tasks'
          AND type IN ('index', 'trigger')
          AND sql IS NOT NULL
        ORDER BY type, name
        """
    ).fetchall()
    foreign_keys_enabled = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    columns_sql = ", ".join(_TASK_COLUMNS)

    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_TASKS_TABLE_SQL)
        connection.execute(
            f"INSERT INTO knowledge_tasks_new ({columns_sql}) "
            f"SELECT {columns_sql} FROM knowledge_tasks"
        )
        source_count = connection.execute("SELECT COUNT(*) FROM knowledge_tasks").fetchone()[0]
        destination_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_tasks_new"
        ).fetchone()[0]
        if source_count != destination_count:
            raise sqlite3.IntegrityError("knowledge_tasks row count mismatch")
        connection.execute("DROP TABLE knowledge_tasks")
        connection.execute("ALTER TABLE knowledge_tasks_new RENAME TO knowledge_tasks")
        for _, statement in schema_objects:
            connection.execute(statement)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("knowledge_tasks foreign key check failed")
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        setting = "ON" if foreign_keys_enabled else "OFF"
        connection.execute(f"PRAGMA foreign_keys={setting}")
    return True


def refusal_active_key(question: str, document_id: int | None) -> str:
    normalized_question = " ".join(question.casefold().split())
    if not normalized_question:
        raise ValueError("refusal question is required")
    raw = f"insufficient:{normalized_question}:{document_id or 0}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TaskService:
    def __init__(self, db: UserDB):
        self._db = db

    def record_feedback(self, feedback_id: int) -> int | None:
        """Create or increment the task a feedback row triggers.

        ``outdated`` feedback creates a task immediately for the document
        owner. ``not_useful`` feedback creates a task only after
        ``feedback_cluster_users`` distinct users report the same normalized
        question/document pair within ``feedback_cluster_days``.
        """
        feedback = self._db.get_feedback(feedback_id)
        if feedback is None:
            return None
        kind = feedback["kind"]
        if kind == "outdated":
            return self._record_outdated(feedback)
        if kind == "not_useful":
            return self._record_not_useful(feedback)
        return None

    def record_refusal(
        self, reporter_id: int, question: str, document_id: int | None
    ) -> int:
        """Aggregate distinct user refusals for one question/document pair."""
        document = (
            self._db.get_document_permission_by_id(document_id)
            if document_id is not None
            else None
        )
        active_key = refusal_active_key(question, document_id)
        return self._db.record_refusal_task(
            active_key=active_key,
            reporter_id=reporter_id,
            question_key=active_key,
            document_id=document_id,
            document_version_id=document.get("current_version_id") if document else None,
            assigned_to=document.get("owner_id") if document else None,
        )

    def record_conflict(self, conflict: dict) -> int:
        """Create one operations task for each persisted conflict record."""
        document_ids = tuple(sorted(set(conflict["document_ids"])))
        document = (
            self._db.get_document_permission_by_id(document_ids[0])
            if len(document_ids) == 1
            else None
        )
        active_key = f"evidence_conflict:{conflict['id']}"
        return self._db.upsert_active_task(
            task_type="evidence_conflict",
            cluster_key=active_key,
            active_key=active_key,
            document_id=document_ids[0] if len(document_ids) == 1 else None,
            document_version_id=document.get("current_version_id") if document else None,
            assigned_to=document.get("owner_id") if document else None,
            report_count=1,
        )

    def _resolve_document(self, feedback: dict) -> dict | None:
        citation_id = feedback.get("citation_id")
        if not citation_id:
            return None
        citation = self._db.get_answer_citation_by_id(citation_id)
        if citation is None:
            return None
        if (citation.get("source_kind") or "document") != "document":
            return None
        return self._db.get_document_permission_by_id(citation["document_id"])

    def _record_outdated(self, feedback: dict) -> int | None:
        document = self._resolve_document(feedback)
        if document is None:
            return None
        report_count = self._count_outdated_reporters(document["id"])
        return self._db.upsert_active_task(
            task_type="outdated",
            cluster_key=f"outdated:{document['id']}",
            active_key=f"outdated:{document['id']}",
            document_id=document["id"],
            document_version_id=document.get("current_version_id"),
            assigned_to=document.get("owner_id"),
            report_count=report_count,
        )

    def _count_outdated_reporters(self, document_id: int) -> int:
        cutoff = time.time() - settings.feedback_cluster_days * 86400
        with self._db._lock:
            row = self._db._conn.execute(
                "SELECT COUNT(DISTINCT f.user_id) AS cnt FROM feedback f "
                "JOIN answer_citations c ON c.id = f.citation_id "
                "WHERE c.source_kind = 'document' AND c.document_id = ? "
                "AND f.kind = 'outdated' AND f.created_at >= ?",
                (document_id, cutoff),
            ).fetchone()
        return int(row["cnt"])

    def _record_not_useful(self, feedback: dict) -> int | None:
        document = self._resolve_document(feedback)
        document_id = document["id"] if document else None
        owner_id = document["owner_id"] if document else None
        question_hash = feedback.get("question_hash") or ""
        distinct = self._db.count_distinct_feedback_users(
            question_hash, settings.feedback_cluster_days
        )
        if distinct < settings.feedback_cluster_users:
            return None
        return self._db.upsert_active_task(
            task_type="not_useful",
            cluster_key=f"not_useful:{question_hash}:{document_id}",
            active_key=f"not_useful:{question_hash}:{document_id}",
            document_id=document_id,
            document_version_id=document["current_version_id"] if document else None,
            assigned_to=owner_id,
            report_count=distinct,
        )

    def transition(
        self,
        task_id: int,
        actor: dict,
        new_status: str,
        note: str,
        expected_updated_at: float,
    ) -> dict:
        task = self._db.get_knowledge_task(task_id)
        if task is None:
            raise KeyError("task not found")
        if new_status not in _ALLOWED_TRANSITIONS.get(task["status"], set()):
            raise ValueError(f"invalid transition: {task['status']} -> {new_status}")
        self._assert_task_actor(task, actor)
        result = self._db.transition_knowledge_task(
            task_id, actor["id"], new_status, note, expected_updated_at
        )
        if result is None:
            raise VersionConflict("task_state_conflict")
        return result

    def reassign(self, task_id: int, actor: dict, assignee_id: int | None) -> dict:
        if not actor.get("is_admin"):
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="需要管理员权限")
        self._db.reassign_knowledge_task(task_id, assignee_id)
        result = self._db.get_knowledge_task(task_id)
        if result is None:
            raise KeyError("task not found")
        return result

    @staticmethod
    def _assert_task_actor(task: dict, actor: dict) -> None:
        """Only the assignee or an administrator may transition a task."""
        is_assignee = task.get("assigned_to") is not None and task["assigned_to"] == actor.get("id")
        if not is_assignee and not actor.get("is_admin"):
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="无权操作该任务")
