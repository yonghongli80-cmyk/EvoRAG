"""Deterministic knowledge-task rules, state transitions, and reassignment."""

from __future__ import annotations

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

    def _resolve_document(self, feedback: dict) -> dict | None:
        citation_id = feedback.get("citation_id")
        if not citation_id:
            return None
        citation = self._db.get_answer_citation_by_id(citation_id)
        if citation is None:
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
                "WHERE c.document_id = ? AND f.kind = 'outdated' AND f.created_at >= ?",
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
        if new_status == "confirmed_expired":
            version_id = task.get("document_version_id")
            if version_id is not None:
                self._db.set_version_validity(version_id, "confirmed_outdated")
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
