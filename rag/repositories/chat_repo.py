"""Conversations, messages, feedback, citations, and knowledge tasks repository."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from config import settings
from rag.repositories.base import BaseRepository, VersionConflict


class ChatRepository(BaseRepository):
    """Handles chat conversations, messages, reliability, citations, and feedback tasks."""

    def create_conversation(self, user_id: int | str, title: str = "", mode: str = "file") -> int:
        """Create a conversation and return its id."""
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO conversations (user_id, title, mode) VALUES (?, ?, ?)",
                (int(user_id), title, mode),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_conversations(self, user_id: int | str, mode: str | None = None) -> list[dict[str, Any]]:
        """Return all conversations for *user_id*, newest first."""
        uid = int(user_id)
        with self.cm.read_connection() as conn:
            if mode is not None:
                rows = conn.execute(
                    "SELECT id, user_id, title, mode, created_at "
                    "FROM conversations WHERE user_id = ? AND mode = ? "
                    "ORDER BY created_at DESC",
                    (uid, mode),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, user_id, title, mode, created_at "
                    "FROM conversations WHERE user_id = ? ORDER BY created_at DESC",
                    (uid,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, conversation_id: int, user_id: int | str) -> dict[str, Any] | None:
        """获取对话（仅 owner 可见）。"""
        uid = int(user_id)
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, user_id, title, mode, created_at FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, uid),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def delete_conversation(self, conversation_id: int, user_id: int | str) -> bool:
        """Delete a conversation (only if owned by user_id). Returns True if deleted."""
        uid = int(user_id)
        with self.cm.write_connection() as conn:
            conn.execute(
                "DELETE FROM chat_messages WHERE conversation_id = ? "
                "AND conversation_id IN (SELECT id FROM conversations WHERE id = ? AND user_id = ?)",
                (conversation_id, conversation_id, uid),
            )
            cur = conn.execute(
                "DELETE FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, uid),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_conversation_title(self, conversation_id: int, user_id: int | str, title: str) -> bool:
        """更新对话标题（仅 owner 可操作）。返回是否成功。"""
        uid = int(user_id)
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
                (title, conversation_id, uid),
            )
            conn.commit()
            return cur.rowcount > 0

    def search_conversations(self, user_id: int | str, q: str, page: int = 1, size: int = 20) -> list[dict[str, Any]]:
        """搜索对话（标题 + 消息内容）。返回匹配的对话列表。"""
        uid = int(user_id)
        offset = (page - 1) * size
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self.cm.read_connection() as conn:
            rows = conn.execute("""
                SELECT DISTINCT c.id, c.title, c.created_at,
                       (SELECT m.content FROM chat_messages m
                        WHERE m.conversation_id = c.id AND m.content LIKE ? ESCAPE '\\'
                        LIMIT 1) as matched_snippet
                FROM conversations c
                LEFT JOIN chat_messages m ON m.conversation_id = c.id
                WHERE c.user_id = ? AND (c.title LIKE ? ESCAPE '\\' OR m.content LIKE ? ESCAPE '\\')
                ORDER BY c.created_at DESC
                LIMIT ? OFFSET ?
            """, (pattern, uid, pattern, pattern, size, offset)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _serialize_reliability(
        confidence_level: str,
        confidence_reasons: tuple[str, ...] | list[str],
        answerability_kind: str,
    ) -> str:
        if confidence_level not in {"high", "medium", "low", "unavailable"}:
            raise ValueError("invalid confidence level")
        if answerability_kind not in {
            "answerable",
            "answerable_with_warning",
            "clarification_required",
            "insufficient_evidence",
            "permission_limited",
        }:
            raise ValueError("invalid answerability kind")
        if not all(isinstance(reason, str) for reason in confidence_reasons):
            raise ValueError("confidence reasons must be strings")
        return json.dumps(list(confidence_reasons), ensure_ascii=False)

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        *,
        confidence_level: str = "unavailable",
        confidence_reasons: tuple[str, ...] | list[str] = (),
        answerability_kind: str = "answerable",
    ) -> int:
        """Append a message to a conversation and return its id."""
        reasons_json = self._serialize_reliability(
            confidence_level, confidence_reasons, answerability_kind
        )
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO chat_messages "
                "(conversation_id, role, content, confidence_level, "
                "confidence_reasons_json, answerability_kind) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    role,
                    content,
                    confidence_level,
                    reasons_json,
                    answerability_kind,
                ),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_messages(self, conversation_id: int, user_id: int | str | None = None) -> list[dict[str, Any]]:
        """Return all messages in a conversation."""
        with self.cm.read_connection() as conn:
            if user_id is not None:
                rows = conn.execute(
                    "SELECT m.id, m.conversation_id, m.role, m.content, "
                    "m.confidence_level, m.confidence_reasons_json, "
                    "m.answerability_kind, m.created_at "
                    "FROM chat_messages m "
                    "JOIN conversations c ON m.conversation_id = c.id "
                    "WHERE m.conversation_id = ? AND c.user_id = ? ORDER BY m.created_at",
                    (conversation_id, int(user_id)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT m.id, m.conversation_id, m.role, m.content, "
                    "m.confidence_level, m.confidence_reasons_json, "
                    "m.answerability_kind, m.created_at "
                    "FROM chat_messages m WHERE m.conversation_id = ? ORDER BY m.created_at",
                    (conversation_id,),
                ).fetchall()
        messages = [dict(r) for r in rows]
        for message in messages:
            try:
                message["confidence_reasons"] = json.loads(
                    message.pop("confidence_reasons_json")
                )
            except (json.JSONDecodeError, TypeError):
                message["confidence_reasons"] = []
        return messages

    def get_message_reliability(
        self, message_id: int, user_id: int | str
    ) -> dict[str, Any] | None:
        """Return only reliability metadata for an assistant message owned by the user."""
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT m.confidence_level, m.confidence_reasons_json, m.answerability_kind "
                "FROM chat_messages m JOIN conversations c ON m.conversation_id = c.id "
                "WHERE m.id = ? AND m.role = 'assistant' AND c.user_id = ?",
                (message_id, int(user_id)),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["confidence_reasons"] = json.loads(
                result.pop("confidence_reasons_json")
            )
        except (json.JSONDecodeError, TypeError):
            result["confidence_reasons"] = []
        return result

    def update_message(self, message_id: int, new_content: str, user_id: int | None = None) -> None:
        """更新消息内容（用于重新生成）。可选 user_id 校验所有权。"""
        with self.cm.write_connection() as conn:
            if user_id is not None:
                conn.execute(
                    "UPDATE chat_messages SET content = ? WHERE id = ? "
                    "AND conversation_id IN (SELECT id FROM conversations WHERE user_id = ?)",
                    (new_content, message_id, user_id),
                )
            else:
                conn.execute(
                    "UPDATE chat_messages SET content = ? WHERE id = ?",
                    (new_content, message_id),
                )
            conn.commit()

    def add_feedback(
        self,
        message_id: int,
        user_id: int | str,
        value: int,
        comment: str = "",
    ) -> int:
        """Record user feedback on a message and return feedback id."""
        if value not in {-1, 0, 1}:
            raise ValueError("feedback value 必须为 -1、0 或 1")
        with self.cm.write_connection() as conn:
            owned = conn.execute(
                "SELECT 1 FROM chat_messages m "
                "JOIN conversations c ON m.conversation_id = c.id "
                "WHERE m.id = ? AND c.user_id = ?",
                (message_id, int(user_id)),
            ).fetchone()
            if owned is None:
                raise ValueError("消息不存在或无权操作")
            cur = conn.execute(
                "INSERT OR REPLACE INTO feedback (message_id, user_id, value, comment) VALUES (?, ?, ?, ?)",
                (message_id, int(user_id), value, comment),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def message_belongs_to_user(self, message_id: int, user_id: int | str) -> bool:
        """Check if a message belongs to a conversation owned by user_id."""
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM chat_messages m "
                "JOIN conversations c ON m.conversation_id = c.id "
                "WHERE m.id = ? AND c.user_id = ?",
                (message_id, int(user_id)),
            ).fetchone()
        return row is not None

    def get_message_question(self, assistant_message_id: int) -> str:
        """Return the user question preceding an assistant message, if any."""
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT content FROM chat_messages "
                "WHERE conversation_id = (SELECT conversation_id FROM chat_messages WHERE id = ?) "
                "AND role = 'user' AND id < ? ORDER BY id DESC LIMIT 1",
                (assistant_message_id, assistant_message_id),
            ).fetchone()
        return row["content"] if row else ""

    def add_knowledge_feedback(
        self,
        message_id: int,
        user_id: int | str,
        kind: str,
        question_text: str,
        question_hash: str,
        citation_id: int | None,
        suggested_answer: str,
        comment: str = "",
    ) -> int:
        """Insert or replace one structured feedback row and return its id."""
        value = 1 if kind == "useful" else -1
        with self.cm.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO feedback "
                "(message_id, user_id, value, comment, kind, question_text, question_hash, "
                "citation_id, suggested_answer, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted')",
                (
                    message_id,
                    int(user_id),
                    value,
                    comment,
                    kind,
                    question_text,
                    question_hash,
                    citation_id,
                    suggested_answer,
                ),
            )
            row = conn.execute(
                "SELECT id FROM feedback WHERE message_id = ? AND user_id = ?",
                (message_id, int(user_id)),
            ).fetchone()
            return int(row["id"])

    def list_user_feedback(
        self, user_id: int | str, page: int = 1, page_size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        """Return the current user's feedback rows plus the total count."""
        uid = int(user_id)
        offset = (page - 1) * page_size
        with self.cm.read_connection() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM feedback WHERE user_id = ?", (uid,)
            ).fetchone()
            rows = conn.execute(
                "SELECT f.id, f.message_id, f.kind, f.question_text, f.question_hash, "
                "f.citation_id, f.comment, f.suggested_answer, f.status, f.created_at, "
                "u.username AS submitter_name "
                "FROM feedback f JOIN users u ON u.id = f.user_id "
                "WHERE f.user_id = ? ORDER BY f.created_at DESC LIMIT ? OFFSET ?",
                (uid, page_size, offset),
            ).fetchall()
        return [dict(r) for r in rows], int(total_row["cnt"])

    def get_feedback(self, feedback_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, message_id, user_id, kind, question_text, question_hash, "
                "citation_id, suggested_answer, comment, status, created_at "
                "FROM feedback WHERE id = ?",
                (feedback_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_answer_citation_by_id(self, citation_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM answer_citations WHERE id = ?", (citation_id,)
            ).fetchone()
        return dict(row) if row else None

    def count_distinct_feedback_users(self, question_hash: str, days: int) -> int:
        cutoff = time.time() - days * 86400
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT user_id) AS cnt FROM feedback "
                "WHERE kind = 'not_useful' AND question_hash = ? AND created_at >= ?",
                (question_hash, cutoff),
            ).fetchone()
        return int(row["cnt"])

    def upsert_active_task(
        self,
        *,
        task_type: str,
        cluster_key: str,
        active_key: str,
        document_id: int | None,
        document_version_id: int | None,
        assigned_to: int | None,
        report_count: int,
    ) -> int:
        with self.cm.transaction(immediate=True):
            return self._upsert_active_task_in_tx(
                task_type=task_type,
                cluster_key=cluster_key,
                active_key=active_key,
                document_id=document_id,
                document_version_id=document_version_id,
                assigned_to=assigned_to,
                report_count=report_count,
            )

    def _upsert_active_task_in_tx(
        self,
        *,
        task_type: str,
        cluster_key: str,
        active_key: str,
        document_id: int | None,
        document_version_id: int | None,
        assigned_to: int | None,
        report_count: int,
    ) -> int:
        now = time.time()
        due_at = now + settings.knowledge_task_due_days * 86400
        conn = self.cm.get_write_connection()
        row = conn.execute(
            "SELECT id FROM knowledge_tasks WHERE active_key = ? "
            "AND status IN ('open', 'in_progress')",
            (active_key,),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE knowledge_tasks SET report_count = ?, updated_at = ? "
                "WHERE id = ?",
                (report_count, now, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO knowledge_tasks "
            "(task_type, cluster_key, active_key, document_id, document_version_id, "
            "assigned_to, status, report_count, due_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
            (
                task_type,
                cluster_key,
                active_key,
                document_id,
                document_version_id,
                assigned_to,
                report_count,
                due_at,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def record_refusal_task(
        self,
        *,
        active_key: str,
        reporter_id: int,
        question_key: str,
        document_id: int | None,
        document_version_id: int | None,
        assigned_to: int | None,
    ) -> int:
        now = time.time()
        with self.cm.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO knowledge_task_refusal_reports (
                    active_key, user_id, question_key, document_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(active_key, user_id) DO UPDATE SET created_at = excluded.created_at
                """,
                (active_key, reporter_id, question_key, document_id, now),
            )
            report_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM knowledge_task_refusal_reports WHERE active_key = ?",
                    (active_key,),
                ).fetchone()[0]
            )
            return self._upsert_active_task_in_tx(
                task_type="insufficient_evidence",
                cluster_key=active_key,
                active_key=active_key,
                document_id=document_id,
                document_version_id=document_version_id,
                assigned_to=assigned_to,
                report_count=report_count,
            )

    def get_knowledge_task(self, task_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_tasks_by_type(self, task_type: str) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_tasks WHERE task_type = ? ORDER BY id", (task_type,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_tasks_for_user(
        self, user_id: int | str, page: int = 1, page_size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        uid = int(user_id)
        offset = (page - 1) * page_size
        with self.cm.read_connection() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks WHERE assigned_to = ?",
                (uid,),
            ).fetchone()
            rows = conn.execute(
                "SELECT * FROM knowledge_tasks WHERE assigned_to = ? "
                "ORDER BY due_at ASC LIMIT ? OFFSET ?",
                (uid, page_size, offset),
            ).fetchall()
        return [dict(r) for r in rows], int(total["cnt"])

    def list_tasks_admin(
        self,
        status: str | None = None,
        assignee_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        offset = (page - 1) * page_size
        conditions = []
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if assignee_id is not None:
            conditions.append("assigned_to = ?")
            params.append(assignee_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.cm.read_connection() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM knowledge_tasks {where}", params
            ).fetchone()
            rows = conn.execute(
                f"SELECT * FROM knowledge_tasks {where} ORDER BY due_at ASC LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall()
        return [dict(r) for r in rows], int(total["cnt"])

    def transition_knowledge_task(
        self,
        task_id: int,
        actor_id: int,
        new_status: str,
        note: str = "",
        expected_updated_at: float | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self.cm.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT status, updated_at FROM knowledge_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            old_status = row["status"]
            resolved_at = now if new_status in {"resolved", "dismissed", "confirmed_expired"} else None
            if expected_updated_at is not None:
                cur = conn.execute(
                    "UPDATE knowledge_tasks SET status = ?, active_key = NULL, "
                    "resolution_note = ?, updated_at = ?, resolved_at = ? "
                    "WHERE id = ? AND updated_at = ?",
                    (new_status, note, now, resolved_at, task_id, expected_updated_at),
                )
            else:
                cur = conn.execute(
                    "UPDATE knowledge_tasks SET status = ?, active_key = NULL, "
                    "resolution_note = ?, updated_at = ?, resolved_at = ? "
                    "WHERE id = ?",
                    (new_status, note, now, resolved_at, task_id),
                )
            if cur.rowcount == 0:
                raise VersionConflict("task_state_conflict")
            conn.execute(
                "INSERT INTO knowledge_task_events "
                "(task_id, actor_id, old_status, new_status, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, actor_id, old_status, new_status, note, now),
            )
        return self.get_knowledge_task(task_id)

    def create_knowledge_conflict(
        self,
        *,
        active_key: str,
        subject_key: str,
        attribute_key: str,
        unit_key: str,
        scope_key: str,
        citation_ids: tuple[int, ...] | list[int],
        document_ids: tuple[int, ...] | list[int],
        document_version_ids: tuple[int, ...] | list[int],
    ) -> dict[str, Any]:
        """Create one active conflict, or return its idempotent existing record."""
        now = time.time()
        citation_ids_json = json.dumps(sorted(set(citation_ids)))
        document_ids_json = json.dumps(sorted(set(document_ids)))
        document_version_ids_json = json.dumps(sorted(set(document_version_ids)))
        with self.cm.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT id FROM knowledge_conflicts WHERE active_key = ? AND status = 'open'",
                (active_key,),
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO knowledge_conflicts "
                    "(active_key, subject_key, attribute_key, unit_key, scope_key, "
                    "citation_ids_json, document_ids_json, document_version_ids_json, "
                    "status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                    (
                        active_key,
                        subject_key,
                        attribute_key,
                        unit_key,
                        scope_key,
                        citation_ids_json,
                        document_ids_json,
                        document_version_ids_json,
                        now,
                        now,
                    ),
                )
                conflict_id = int(cur.lastrowid)
            else:
                conflict_id = int(row["id"])
        conflict = self.get_knowledge_conflict(conflict_id)
        if conflict is None:
            raise RuntimeError("knowledge conflict was not persisted")
        return conflict

    def get_knowledge_conflict(self, conflict_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_conflicts WHERE id = ?", (conflict_id,)
            ).fetchone()
        if row is None:
            return None
        conflict = dict(row)
        for field in ("citation_ids", "document_ids", "document_version_ids"):
            conflict[field] = tuple(json.loads(conflict.pop(f"{field}_json")))
        return conflict

    def resolve_knowledge_conflict(
        self,
        conflict_id: int,
        *,
        actor_id: int,
        resolution: str,
        note: str,
    ) -> dict[str, Any] | None:
        """Resolve an active conflict only for an admin or every document owner."""
        if resolution not in {"resolved", "dismissed"}:
            raise ValueError("unsupported conflict resolution")

        now = time.time()
        with self.cm.transaction(immediate=True) as conn:
            conflict = conn.execute(
                "SELECT status, document_ids_json FROM knowledge_conflicts WHERE id = ?",
                (conflict_id,),
            ).fetchone()
            if conflict is None:
                return None

            actor = conn.execute(
                "SELECT is_admin FROM users WHERE id = ?", (actor_id,)
            ).fetchone()
            if actor is None:
                raise PermissionError("not authorized to resolve conflict")

            document_ids = tuple(json.loads(conflict["document_ids_json"]))
            if not bool(actor["is_admin"]):
                if not document_ids:
                    raise PermissionError("not authorized to resolve conflict")
                placeholders = ",".join("?" for _ in document_ids)
                owners = conn.execute(
                    f"SELECT id, owner_id FROM document_permissions WHERE id IN ({placeholders})",
                    document_ids,
                ).fetchall()
                if len(owners) != len(document_ids) or any(
                    int(row["owner_id"]) != actor_id for row in owners
                ):
                    raise PermissionError("not authorized to resolve conflict")

            old_status = str(conflict["status"])
            if old_status != "open":
                raise VersionConflict("conflict_state_conflict")
            conn.execute(
                "UPDATE knowledge_conflicts SET status = ?, active_key = NULL, resolution = ?, "
                "resolution_note = ?, resolved_by = ?, updated_at = ?, resolved_at = ? "
                "WHERE id = ?",
                (resolution, resolution, note, actor_id, now, now, conflict_id),
            )
            conn.execute(
                "INSERT INTO knowledge_conflict_events "
                "(conflict_id, actor_id, old_status, new_status, resolution, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conflict_id, actor_id, old_status, resolution, resolution, note, now),
            )
        return self.get_knowledge_conflict(conflict_id)

    def list_knowledge_conflict_events(self, conflict_id: int) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT actor_id, old_status, new_status, resolution, note, created_at "
                "FROM knowledge_conflict_events WHERE conflict_id = ? ORDER BY id ASC",
                (conflict_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reassign_knowledge_task(self, task_id: int, assignee_id: int | None) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE knowledge_tasks SET assigned_to = ? WHERE id = ?",
                (assignee_id, task_id),
            )
            conn.commit()

    def set_document_owner(self, document_id: int, owner_id: int) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE document_permissions SET owner_id = ? WHERE id = ?",
                (owner_id, document_id),
            )
            conn.commit()

    def list_task_reporters(self, task_id: int) -> list[str]:
        task = self.get_knowledge_task(task_id)
        if task is None:
            return []
        cutoff = task["created_at"] - settings.feedback_cluster_days * 86400
        with self.cm.read_connection() as conn:
            if task["task_type"] == "outdated":
                rows = conn.execute(
                    "SELECT DISTINCT u.username FROM feedback f "
                    "JOIN answer_citations c ON c.id = f.citation_id "
                    "JOIN users u ON u.id = f.user_id "
                    "WHERE c.document_id = ? AND f.kind = 'outdated' AND f.created_at >= ?",
                    (task["document_id"], cutoff),
                ).fetchall()
            else:
                prefix = "not_useful:"
                qh = task["cluster_key"].split(":")[1] if task["cluster_key"].startswith(prefix) else None
                if qh is None:
                    return []
                rows = conn.execute(
                    "SELECT DISTINCT u.username FROM feedback f JOIN users u ON u.id = f.user_id "
                    "WHERE f.kind = 'not_useful' AND f.question_hash = ? AND f.created_at >= ?",
                    (qh, cutoff),
                ).fetchall()
        return [r["username"] for r in rows]

    def task_summary(self) -> dict[str, Any]:
        now = time.time()
        with self.cm.read_connection() as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks WHERE status = 'open'"
            ).fetchone()["cnt"]
            overdue_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks "
                "WHERE status IN ('open', 'in_progress') AND due_at < ?",
                (now,),
            ).fetchone()["cnt"]
            outdated_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks WHERE task_type = 'outdated' "
                "AND status IN ('open', 'in_progress')"
            ).fetchone()["cnt"]
            not_useful_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks WHERE task_type = 'not_useful' "
                "AND status IN ('open', 'in_progress')"
            ).fetchone()["cnt"]
            stale_cutoff = now - settings.knowledge_stale_days * 86400
            stale_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM ("
                "SELECT kb_id FROM document_permissions "
                "WHERE deleted_at IS NULL AND current_version_id IS NOT NULL "
                "GROUP BY kb_id HAVING MAX(updated_at) < ?)",
                (stale_cutoff,),
            ).fetchone()["cnt"]
        return {
            "open": int(open_count),
            "overdue": int(overdue_count),
            "outdated": int(outdated_count),
            "not_useful": int(not_useful_count),
            "stale_knowledge_bases": int(stale_count),
        }

    def _insert_message_in_tx(
        self,
        conversation_id: int,
        role: str,
        content: str,
        persistence_key: str | None = None,
        citations_hash: str | None = None,
        citation_count: int = 0,
        confidence_level: str = "unavailable",
        confidence_reasons_json: str = "[]",
        answerability_kind: str = "answerable",
    ) -> int:
        """Insert one message row inside an already-open transaction."""
        conn = self.cm.get_write_connection()
        cur = conn.execute(
            "INSERT INTO chat_messages "
            "(conversation_id, role, content, persistence_key, citations_hash, "
            "citation_count, confidence_level, confidence_reasons_json, "
            "answerability_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                role,
                content,
                persistence_key,
                citations_hash,
                citation_count,
                confidence_level,
                confidence_reasons_json,
                answerability_kind,
            ),
        )
        return int(cur.lastrowid)

    def _insert_answer_citations_in_tx(self, assistant_message_id: int, citations: list) -> None:
        """Insert citation rows inside an already-open transaction."""
        conn = self.cm.get_write_connection()
        for citation in citations:
            conn.execute(
                "INSERT INTO answer_citations "
                "(assistant_message_id, document_id, document_version_id, chunk_index, "
                "section_title, text_preview, rank, is_historical) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    assistant_message_id,
                    citation.document_id,
                    citation.document_version_id,
                    citation.chunk_index,
                    citation.section_title,
                    citation.text_preview,
                    citation.rank,
                    int(citation.is_historical),
                ),
            )

    def persist_assistant_answer_with_citations(
        self,
        conversation_id: int,
        answer: str,
        citations: list,
        persistence_key: str,
        *,
        confidence_level: str = "unavailable",
        confidence_reasons: tuple[str, ...] | list[str] = (),
        answerability_kind: str = "answerable",
    ) -> int:
        """Persist one assistant answer and its citations atomically."""
        citations_hash = self._citations_hash(citations)
        reasons_json = self._serialize_reliability(
            confidence_level, confidence_reasons, answerability_kind
        )
        with self.cm.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT id, content, citations_hash, citation_count, confidence_level, "
                "confidence_reasons_json, answerability_kind FROM chat_messages "
                "WHERE conversation_id = ? AND persistence_key = ?",
                (conversation_id, persistence_key),
            ).fetchone()
            if row is not None:
                if (
                    row["content"] == answer
                    and row["citations_hash"] == citations_hash
                    and row["citation_count"] == len(citations)
                    and row["confidence_level"] == confidence_level
                    and row["confidence_reasons_json"] == reasons_json
                    and row["answerability_kind"] == answerability_kind
                ):
                    return int(row["id"])
                raise VersionConflict("answer persistence idempotency conflict")
            insert_msg_fn = getattr(self.db, "_insert_message_in_tx", self._insert_message_in_tx) if self.db is not None else self._insert_message_in_tx
            message_id = insert_msg_fn(
                conversation_id,
                "assistant",
                answer,
                persistence_key,
                citations_hash,
                len(citations),
                confidence_level,
                reasons_json,
                answerability_kind,
            )
            insert_cit_fn = getattr(self.db, "_insert_answer_citations_in_tx", self._insert_answer_citations_in_tx) if self.db is not None else self._insert_answer_citations_in_tx
            insert_cit_fn(message_id, citations)
            return message_id

    @staticmethod
    def _citations_hash(citations: list) -> str:
        rows = sorted(
            (
                c.document_id,
                c.document_version_id,
                c.chunk_index,
                c.section_title,
                c.text_preview,
                c.rank,
                int(c.is_historical),
            )
            for c in citations
        )
        digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
        return digest.hexdigest()

    def get_answer_citations(self, assistant_message_id: int) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT c.id, c.document_id, c.document_version_id, c.chunk_index, "
                "c.section_title, c.text_preview, c.rank, c.is_historical, c.created_at, "
                "v.version_no, v.original_filename, v.created_at AS version_created_at "
                "FROM answer_citations c "
                "LEFT JOIN document_versions v ON v.id = c.document_version_id "
                "WHERE c.assistant_message_id = ? ORDER BY c.rank",
                (assistant_message_id,),
            ).fetchall()
        return [dict(r) for r in rows]
