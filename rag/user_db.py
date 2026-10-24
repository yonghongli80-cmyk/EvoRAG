"""SQLite-backed user database facade decomposing into domain repositories."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from config import settings
from rag.db.connection import ConnectionManager
from rag.repositories import (
    AnalysisRepository,
    ChatRepository,
    DocRepository,
    MaterialRepository,
    SpreadsheetRepository,
    UserRepository,
    VersionConflict,
    _validate_password_strength,
    canonical_json_text,
    citations_hash,
    create_material_schema,
    create_tables,
    mask_data_source_config,
    rebuild_document_versions_status,
    serialize_reliability,
)
from rag.repositories.analysis_repo import (
    _QUERY_SECRET_PARAMS,
    _SENSITIVE_CONFIG_HINTS,
    _mask_url_credentials,
)
from rag.repositories.kb_lifecycle_repo import KBLifecycleRepository

__all__ = [
    "UserDB",
    "VersionConflict",
    "_validate_password_strength",
    "mask_data_source_config",
    "_mask_url_credentials",
    "_SENSITIVE_CONFIG_HINTS",
    "_QUERY_SECRET_PARAMS",
]


class UserDB:
    """Thread-safe SQLite database facade delegating to domain sub-repositories."""

    _DOCUMENT_PERMISSION_COLUMNS = DocRepository._DOCUMENT_PERMISSION_COLUMNS

    def __init__(
        self,
        db_path: str | Path | None = None,
        connection_manager: ConnectionManager | None = None,
    ) -> None:
        if connection_manager is not None:
            self.cm = connection_manager
            self.db_path = str(connection_manager.db_path)
        else:
            if db_path is None:
                db_path = str(settings.users_db_path)
            self.db_path = str(db_path)
            self.cm = ConnectionManager(self.db_path, busy_timeout_ms=30000, timeout=30.0)

        self._cm = self.cm
        self._lock = self.cm._write_lock
        self._conn = self.cm.get_write_connection()
        self._create_tables()

        from rag.db_migrations import run_migrations
        from rag.knowledge_tasks import migrate_knowledge_task_types
        from rag.org_auth.schema import install_org_schema

        run_migrations(self._conn)
        migrate_knowledge_task_types(self._conn)
        with self.locked_transaction():
            install_org_schema(self._conn)

        self.user_repo = UserRepository(self.cm, db=self)
        self.doc_repo = DocRepository(self.cm, db=self)
        self.chat_repo = ChatRepository(self.cm, db=self)
        self.spreadsheet_repo = SpreadsheetRepository(self.cm, db=self)
        self.material_repo = MaterialRepository(self.cm, db=self)
        self.analysis_repo = AnalysisRepository(self.cm, db=self)
        self.kb_lifecycle_repo = KBLifecycleRepository(self.cm, db=self)

    # ------------------------------------------------------------------
    # Transaction helpers & Lifecycle
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self, immediate: bool = False):
        with self.cm.transaction(immediate=immediate) as conn:
            yield conn

    @contextmanager
    def locked_transaction(self, immediate: bool = False):
        with self.cm.transaction(immediate=immediate) as conn:
            yield conn

    @contextmanager
    def locked_read(self):
        with self.cm.read_connection() as conn:
            yield conn

    def close(self) -> None:
        self.cm.close()

    def _create_tables(self) -> None:
        create_tables(self._conn)
        # Material/job tables live in a separate schema for historical reasons;
        # a fresh database that skips this call has no document_index_jobs and
        # the wiki retry enqueue silently fails. Keep the two in one place.
        create_material_schema(self._conn)

    def _rebuild_document_versions_status(self) -> None:
        rebuild_document_versions_status(self._conn)

    def create_material_schema(self) -> None:
        create_material_schema(self._conn)

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_reliability(
        confidence_level: str,
        confidence_reasons: tuple[str, ...] | list[str],
        answerability_kind: str,
    ) -> str:
        return serialize_reliability(confidence_level, confidence_reasons, answerability_kind)

    @staticmethod
    def _citations_hash(citations: list) -> str:
        return citations_hash(citations)

    @staticmethod
    def _canonical_json_text(value: Any) -> str:
        return canonical_json_text(value)

    def search_users(self, query: str, limit: int=20) -> list[dict[str, Any]]:
        """按用户名搜索用户。返回 [{id, username}]，最多 limit 条。"""
        return self.user_repo.search_users(query, limit)

    def change_password(self, user_id: int, old_password: str, new_password: str) -> None:
        """修改密码。验证旧密码、新密码强度、新旧密码不同。"""
        return self.user_repo.change_password(user_id, old_password, new_password)

    def reset_password(self, user_id: int, new_password: str) -> None:
        """重置密码（不验证旧密码）。仅 admin 调用。"""
        return self.user_repo.reset_password(user_id, new_password)

    def create_kb_metadata(self, kb_id: str, name: str, owner_id: int=0, scope: str='private') -> None:
        """创建 KB 元数据（仅新 KB 调用，不覆盖已有数据）。"""
        return self.doc_repo.create_kb_metadata(kb_id, name, owner_id, scope)

    def get_kb_metadata(self, kb_id: str) -> dict[str, Any] | None:
        """获取 KB 元数据。不存在返回 None。"""
        return self.doc_repo.get_kb_metadata(kb_id)

    def get_kb_lifecycle(self, kb_id: str) -> dict[str, Any] | None:
        return self.kb_lifecycle_repo.get(kb_id)

    def create_kb_document_pending(self, kb_id: str, filename: str) -> int:
        return self.doc_repo.create_kb_document_pending(kb_id, filename)

    def finish_kb_document(self, document_id: int, file_path: str, toc: str, summary: str, chunk_count: int) -> None:
        return self.doc_repo.finish_kb_document(document_id, file_path, toc, summary, chunk_count)

    def set_kb_document_state(self, document_id: int, status: str, last_error: str='') -> None:
        return self.doc_repo.set_kb_document_state(document_id, status, last_error)

    def get_kb_document(self, kb_id: str, filename: str) -> dict[str, Any] | None:
        return self.doc_repo.get_kb_document(kb_id, filename)

    def list_kb_documents(self, kb_id: str) -> list[dict[str, Any]]:
        return self.doc_repo.list_kb_documents(kb_id)

    def delete_kb_document(self, document_id: int) -> None:
        return self.doc_repo.delete_kb_document(document_id)

    def update_kb_scope(self, kb_id: str, scope: str) -> None:
        """更新 KB 的 scope 字段。scope 必须是 'private' 或 'public'。"""
        return self.doc_repo.update_kb_scope(kb_id, scope)

    def update_kb_overview(self, kb_id: str, overview: str) -> bool:
        """Update an existing knowledge base overview through the shared connection."""
        return self.doc_repo.update_kb_overview(kb_id, overview)

    def update_kb_document_toc(self, kb_id: str, filename: str, toc: str) -> bool:
        return self.doc_repo.update_kb_document_toc(kb_id, filename, toc)

    def update_kb_document_summary(self, kb_id: str, filename: str, summary: str) -> bool:
        return self.doc_repo.update_kb_document_summary(kb_id, filename, summary)

    def get_kb_metadata_by_names(self, kb_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """批量查询 KB 元数据。返回 {kb_id: meta_dict_or_None}。"""
        return self.doc_repo.get_kb_metadata_by_names(kb_ids)

    def create_user(self, username: str, password: str) -> int:
        """Create a new user.  Raises ``ValueError`` if the username is taken."""
        return self.user_repo.create_user(username, password)

    def ensure_development_user(self) -> dict[str, Any]:
        """Ensure the auth-disabled principal has a real FK-safe user row."""
        return self.user_repo.ensure_development_user()

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """Return user dict on success, ``None`` on failure."""
        return self.user_repo.authenticate(username, password)

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Return user dict or ``None``."""
        return self.user_repo.get_user_by_id(user_id)

    def create_conversation(self, user_id: int | str, title: str='', mode: str='file') -> int:
        """Create a conversation and return its id."""
        return self.chat_repo.create_conversation(user_id, title, mode)

    def list_conversations(self, user_id: int | str, mode: str | None=None) -> list[dict[str, Any]]:
        """Return all conversations for *user_id*, newest first."""
        return self.chat_repo.list_conversations(user_id, mode)

    def get_conversation(self, conversation_id: int, user_id: int | str) -> dict[str, Any] | None:
        """获取对话（仅 owner 可见）。"""
        return self.chat_repo.get_conversation(conversation_id, user_id)

    def delete_conversation(self, conversation_id: int, user_id: int | str) -> bool:
        """Delete a conversation (only if owned by user_id). Returns True if deleted."""
        return self.chat_repo.delete_conversation(conversation_id, user_id)

    def update_conversation_title(self, conversation_id: int, user_id: int | str, title: str) -> bool:
        """更新对话标题（仅 owner 可操作）。返回是否成功。"""
        return self.chat_repo.update_conversation_title(conversation_id, user_id, title)

    def search_conversations(self, user_id: int | str, q: str, page: int=1, size: int=20) -> list[dict[str, Any]]:
        """搜索对话（标题 + 消息内容）。返回匹配的对话列表。"""
        return self.chat_repo.search_conversations(user_id, q, page, size)

    def add_message(self, conversation_id: int, role: str, content: str, *, confidence_level: str='unavailable', confidence_reasons: tuple[str, ...] | list[str]=(), answerability_kind: str='answerable') -> int:
        """Append a message to a conversation and return its id."""
        return self.chat_repo.add_message(conversation_id, role, content, confidence_level=confidence_level, confidence_reasons=confidence_reasons, answerability_kind=answerability_kind)

    def get_messages(self, conversation_id: int, user_id: int | str | None=None) -> list[dict[str, Any]]:
        """Return all messages in a conversation."""
        return self.chat_repo.get_messages(conversation_id, user_id)

    def get_message_reliability(self, message_id: int, user_id: int | str) -> dict[str, Any] | None:
        """Return only reliability metadata for an assistant message owned by the user."""
        return self.chat_repo.get_message_reliability(message_id, user_id)

    def update_message(self, message_id: int, new_content: str, user_id: int=None):
        """更新消息内容（用于重新生成）。可选 user_id 校验所有权。"""
        return self.chat_repo.update_message(message_id, new_content, user_id)

    def add_feedback(self, message_id: int, user_id: int | str, value: int, comment: str='') -> int:
        """Record user feedback on a message and return feedback id."""
        return self.chat_repo.add_feedback(message_id, user_id, value, comment)

    def message_belongs_to_user(self, message_id: int, user_id: int | str) -> bool:
        """Check if a message belongs to a conversation owned by user_id."""
        return self.chat_repo.message_belongs_to_user(message_id, user_id)

    def get_message_question(self, assistant_message_id: int) -> str:
        """Return the user question preceding an assistant message, if any."""
        return self.chat_repo.get_message_question(assistant_message_id)

    def add_knowledge_feedback(self, message_id: int, user_id: int | str, kind: str, question_text: str, question_hash: str, citation_id: int | None, suggested_answer: str, comment: str='') -> int:
        """Insert or replace one structured feedback row and return its id."""
        return self.chat_repo.add_knowledge_feedback(message_id, user_id, kind, question_text, question_hash, citation_id, suggested_answer, comment)

    def list_user_feedback(self, user_id: int | str, page: int=1, page_size: int=20) -> tuple[list[dict[str, Any]], int]:
        """Return the current user's feedback rows plus the total count."""
        return self.chat_repo.list_user_feedback(user_id, page, page_size)

    def get_feedback(self, feedback_id: int) -> dict[str, Any] | None:
        return self.chat_repo.get_feedback(feedback_id)

    def get_answer_citation_by_id(self, citation_id: int) -> dict[str, Any] | None:
        return self.chat_repo.get_answer_citation_by_id(citation_id)

    def count_distinct_feedback_users(self, question_hash: str, days: int) -> int:
        return self.chat_repo.count_distinct_feedback_users(question_hash, days)

    def upsert_active_task(self, *, task_type: str, cluster_key: str, active_key: str, document_id: int | None, document_version_id: int | None, assigned_to: int | None, report_count: int) -> int:
        return self.chat_repo.upsert_active_task(task_type=task_type, cluster_key=cluster_key, active_key=active_key, document_id=document_id, document_version_id=document_version_id, assigned_to=assigned_to, report_count=report_count)

    def _upsert_active_task_in_tx(self, *, task_type: str, cluster_key: str, active_key: str, document_id: int | None, document_version_id: int | None, assigned_to: int | None, report_count: int) -> int:
        return self.chat_repo._upsert_active_task_in_tx(task_type=task_type, cluster_key=cluster_key, active_key=active_key, document_id=document_id, document_version_id=document_version_id, assigned_to=assigned_to, report_count=report_count)

    def record_refusal_task(self, *, active_key: str, reporter_id: int, question_key: str, document_id: int | None, document_version_id: int | None, assigned_to: int | None) -> int:
        return self.chat_repo.record_refusal_task(active_key=active_key, reporter_id=reporter_id, question_key=question_key, document_id=document_id, document_version_id=document_version_id, assigned_to=assigned_to)

    def get_knowledge_task(self, task_id: int) -> dict[str, Any] | None:
        return self.chat_repo.get_knowledge_task(task_id)

    def list_tasks_by_type(self, task_type: str) -> list[dict[str, Any]]:
        return self.chat_repo.list_tasks_by_type(task_type)

    def list_tasks_for_user(self, user_id: int | str, page: int=1, page_size: int=20) -> tuple[list[dict[str, Any]], int]:
        return self.chat_repo.list_tasks_for_user(user_id, page, page_size)

    def list_tasks_admin(self, status: str | None=None, assignee_id: int | None=None, page: int=1, page_size: int=20) -> tuple[list[dict[str, Any]], int]:
        return self.chat_repo.list_tasks_admin(status, assignee_id, page, page_size)

    def transition_knowledge_task(self, task_id: int, actor_id: int, new_status: str, note: str, expected_updated_at: float) -> dict[str, Any] | None:
        return self.chat_repo.transition_knowledge_task(task_id, actor_id, new_status, note, expected_updated_at)

    def create_knowledge_conflict(self, *, active_key: str, subject_key: str, attribute_key: str, unit_key: str, scope_key: str, citation_ids: tuple[int, ...], document_ids: tuple[int, ...], document_version_ids: tuple[int, ...]) -> dict[str, Any]:
        """Create one active conflict, or return its idempotent existing record."""
        return self.chat_repo.create_knowledge_conflict(active_key=active_key, subject_key=subject_key, attribute_key=attribute_key, unit_key=unit_key, scope_key=scope_key, citation_ids=citation_ids, document_ids=document_ids, document_version_ids=document_version_ids)

    def get_knowledge_conflict(self, conflict_id: int) -> dict[str, Any] | None:
        return self.chat_repo.get_knowledge_conflict(conflict_id)

    def resolve_knowledge_conflict(self, conflict_id: int, *, actor_id: int, resolution: str, note: str) -> dict[str, Any] | None:
        """Resolve an active conflict only for an admin or every document owner."""
        return self.chat_repo.resolve_knowledge_conflict(conflict_id, actor_id=actor_id, resolution=resolution, note=note)

    def list_knowledge_conflict_events(self, conflict_id: int) -> list[dict[str, Any]]:
        return self.chat_repo.list_knowledge_conflict_events(conflict_id)

    def reassign_knowledge_task(self, task_id: int, assignee_id: int | None) -> None:
        return self.chat_repo.reassign_knowledge_task(task_id, assignee_id)

    def set_document_owner(self, document_id: int, owner_id: int) -> None:
        return self.chat_repo.set_document_owner(document_id, owner_id)

    def list_task_reporters(self, task_id: int) -> list[str]:
        return self.chat_repo.list_task_reporters(task_id)

    def task_summary(self) -> dict[str, Any]:
        return self.chat_repo.task_summary()

    def _insert_message_in_tx(self, conversation_id: int, role: str, content: str, persistence_key: str | None=None, citations_hash: str | None=None, citation_count: int=0, confidence_level: str='unavailable', confidence_reasons_json: str='[]', answerability_kind: str='answerable') -> int:
        """Insert one message row inside an already-open transaction."""
        return self.chat_repo._insert_message_in_tx(conversation_id, role, content, persistence_key, citations_hash, citation_count, confidence_level, confidence_reasons_json, answerability_kind)

    def _insert_answer_citations_in_tx(self, assistant_message_id: int, citations: list) -> None:
        """Insert citation rows inside an already-open transaction."""
        return self.chat_repo._insert_answer_citations_in_tx(assistant_message_id, citations)

    def persist_assistant_answer_with_citations(self, conversation_id: int, answer: str, citations: list, persistence_key: str, *, confidence_level: str='unavailable', confidence_reasons: tuple[str, ...] | list[str]=(), answerability_kind: str='answerable') -> int:
        """Persist one assistant answer and its citations atomically."""
        return self.chat_repo.persist_assistant_answer_with_citations(conversation_id, answer, citations, persistence_key, confidence_level=confidence_level, confidence_reasons=confidence_reasons, answerability_kind=answerability_kind)

    def get_answer_citations(self, assistant_message_id: int) -> list[dict[str, Any]]:
        return self.chat_repo.get_answer_citations(assistant_message_id)

    def persist_document_comparison(self, owner_id: int, request, execution):
        """Persist comparison output and its independent citations atomically."""
        return self.doc_repo.persist_document_comparison(owner_id, request, execution)

    def get_document_comparison(self, comparison_id: int, actor_id: int, is_admin: bool):
        """Return one stored comparison only to its owner or an administrator."""
        return self.doc_repo.get_document_comparison(comparison_id, actor_id, is_admin)

    def get_document_comparison_document_ids(self, comparison_id: int, actor_id: int, is_admin: bool) -> tuple[int, ...] | None:
        """Return comparison source IDs only after the same ownership check."""
        return self.doc_repo.get_document_comparison_document_ids(comparison_id, actor_id, is_admin)

    def _is_admin(self, user_id: Any) -> bool:
        """判断 *user_id* 是否具备数据源 admin 权限（可读/删全部数据源）。"""
        return self.user_repo._is_admin(user_id)

    def create_data_source(self, name: str, source_type: str, config: str, owner_id: Any='admin') -> int:
        """Create a data source and return its id."""
        return self.analysis_repo.create_data_source(name, source_type, config, owner_id)

    def list_data_sources(self, user_id: Any=None) -> list[dict[str, Any]]:
        """Return data sources visible to *user_id*, newest first."""
        return self.analysis_repo.list_data_sources(user_id)

    def get_data_source(self, source_id: int, user_id: Any=None) -> dict[str, Any] | None:
        """Return a single data source by id, or None."""
        return self.analysis_repo.get_data_source(source_id, user_id)

    def delete_data_source(self, source_id: int, user_id: Any=None) -> bool:
        """Delete a data source.  Returns True if deleted."""
        return self.analysis_repo.delete_data_source(source_id, user_id)

    def update_data_source_synced(self, source_id: int) -> None:
        """Update the last_synced_at timestamp to now."""
        return self.analysis_repo.update_data_source_synced(source_id)

    def update_data_source_status(self, source_id: int, status: str) -> None:
        """Update the status of a data source."""
        return self.analysis_repo.update_data_source_status(source_id, status)

    def create_card(self, user_id: int | str, name: str) -> int:
        """Create an analysis card and return its id."""
        return self.analysis_repo.create_card(user_id, name)

    def list_cards(self, user_id: int | str) -> list[dict[str, Any]]:
        """Return all analysis cards for *user_id*, newest first."""
        return self.analysis_repo.list_cards(user_id)

    def delete_card(self, card_id: int, user_id: int | str) -> bool:
        """Delete an analysis card and its questions. Returns True if deleted."""
        return self.analysis_repo.delete_card(card_id, user_id)

    def rename_card(self, card_id: int, name: str, user_id: int | str) -> bool:
        """Rename an analysis card. Returns True if updated."""
        return self.analysis_repo.rename_card(card_id, name, user_id)

    def get_card_summary(self, card_id: int) -> str:
        """Return the summary text for a card, empty string if none."""
        return self.analysis_repo.get_card_summary(card_id)

    def update_card_summary(self, card_id: int, summary: str, user_id: int | str) -> bool:
        """Update the summary for a card. Returns True if updated."""
        return self.analysis_repo.update_card_summary(card_id, summary, user_id)

    def add_question(self, card_id: int, question: str, answer: str='', source_mode: str='', source_message_id: int | None=None, user_id: int | str | None=None) -> int | None:
        """Add a question to a card and return its id."""
        return self.analysis_repo.add_question(card_id, question, answer, source_mode, source_message_id, user_id)

    def get_questions(self, card_id: int) -> list[dict[str, Any]]:
        """Return all questions for a card, ordered by created_at."""
        return self.analysis_repo.get_questions(card_id)

    def delete_question(self, question_id: int, user_id: int | str) -> bool:
        """Delete a question. Returns True if deleted."""
        return self.analysis_repo.delete_question(question_id, user_id)

    def set_user_admin(self, user_id: int, is_admin: bool) -> None:
        return self.user_repo.set_user_admin(user_id, is_admin)

    def create_document_permission(self, doc_name: str, kb_id: str, owner_id: int, permission_level: int=1, is_public: bool=False, protected: bool=False, scope: str=None) -> int:
        """创建文档权限记录。scope 不传时根据 is_public 自动推断。"""
        return self.doc_repo.create_document_permission(doc_name, kb_id, owner_id, permission_level, is_public, protected, scope)

    def get_document_permission(self, doc_name: str, kb_id: str) -> dict[str, Any] | None:
        return self.doc_repo.get_document_permission(doc_name, kb_id)

    def get_document_permission_by_id(self, doc_id: int) -> dict[str, Any] | None:
        return self.doc_repo.get_document_permission_by_id(doc_id)

    def toggle_document_visibility(self, doc_id: int, is_public: bool | None=None) -> bool:
        """切换文档公开/私有状态，返回新的 is_public 值。"""
        return self.doc_repo.toggle_document_visibility(doc_id, is_public)

    def delete_document_permission(self, doc_id: int) -> None:
        return self.doc_repo.delete_document_permission(doc_id)

    def get_accessible_doc_names(self, kb_id: str, user_id: int, user_level: int=1) -> list[str]:
        """返回用户在指定知识库中有权查看的文档名列表。"""
        return self.doc_repo.get_accessible_doc_names(kb_id, user_id, user_level)

    def get_document_permissions_by_names(self, doc_names: list[str], kb_id: str) -> dict[str, dict[str, Any] | None]:
        """批量查询多个文档的权限记录。返回 {doc_name: perm_dict_or_None}。"""
        return self.doc_repo.get_document_permissions_by_names(doc_names, kb_id)

    def get_document_permissions_by_ids(self, doc_ids: list[int]) -> dict[int, dict[str, Any]]:
        """批量查询多个权限记录（按 ID）。返回 {doc_id: perm_dict}。"""
        return self.doc_repo.get_document_permissions_by_ids(doc_ids)

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """按用户名查询用户。"""
        return self.user_repo.get_user_by_username(username)

    def share_document(self, doc_id: int, user_id: int, granted_by: int, permission: str='view') -> int:
        return self.doc_repo.share_document(doc_id, user_id, granted_by, permission)

    def unshare_document(self, doc_id: int, user_id: int) -> None:
        return self.doc_repo.unshare_document(doc_id, user_id)

    def is_document_shared(self, doc_id: int, user_id: int, permission: str=None) -> bool:
        """检查文档是否共享给指定用户。permission=None 表示任意权限，'view' 或 'edit' 表示特定权限。"""
        return self.doc_repo.is_document_shared(doc_id, user_id, permission)

    def is_kb_shared(self, kb_id: str, user_id: int, permission: str=None) -> bool:
        """检查知识库是否共享给指定用户。permission=None 表示任意权限，'view' 或 'edit' 表示特定权限。"""
        return self.doc_repo.is_kb_shared(kb_id, user_id, permission)

    def list_shared_users(self, doc_id: int) -> list[dict[str, Any]]:
        return self.doc_repo.list_shared_users(doc_id)

    def get_shared_doc_ids_for_user(self, user_id: int, doc_ids: list[int]) -> set[int]:
        """批量查询用户被共享的 doc_id 集合。返回指定 doc_ids 中被共享的子集。"""
        return self.doc_repo.get_shared_doc_ids_for_user(user_id, doc_ids)

    def share_kb(self, kb_id: str, user_id: int, granted_by: int, permission: str='view') -> int:
        """共享 KB 给指定用户，并自动将 scope 设为 'shared'。"""
        return self.doc_repo.share_kb(kb_id, user_id, granted_by, permission)

    def unshare_kb(self, kb_id: str, user_id: int) -> None:
        """取消 KB 共享，如果没有剩余共享用户则自动将 scope 设回 'private'。"""
        return self.doc_repo.unshare_kb(kb_id, user_id)

    def list_kb_shared_users(self, kb_id: str) -> list[dict[str, Any]]:
        return self.doc_repo.list_kb_shared_users(kb_id)

    def create_structured_dataset(self, document_id: int, document_version_id: int, fmt: str, semantic_index_mode: str) -> int:
        """Reserve one staging dataset bound to one immutable document version."""
        return self.spreadsheet_repo.create_structured_dataset(document_id, document_version_id, fmt, semantic_index_mode)

    def activate_structured_dataset(self, dataset_id: int) -> None:
        """Mark a fully validated staging dataset ready without changing document state."""
        return self.spreadsheet_repo.activate_structured_dataset(dataset_id)

    def _activate_structured_dataset_in_tx(self, document_id: int, dataset_id: int) -> None:
        """Promote a staging dataset while its caller owns the SQLite transaction."""
        return self.spreadsheet_repo._activate_structured_dataset_in_tx(document_id, dataset_id)

    def activate_document_and_dataset(self, *, document_id: int, version_id: int, dataset_id: int, kb_id: str, filename: str) -> None:
        """Atomically make a document version and its dataset current and ready."""
        return self.spreadsheet_repo.activate_document_and_dataset(document_id=document_id, version_id=version_id, dataset_id=dataset_id, kb_id=kb_id, filename=filename)

    def mark_structured_dataset_failed(self, dataset_id: int, error_code: str) -> None:
        """Record an extraction failure without altering any current-version pointer."""
        return self.spreadsheet_repo.mark_structured_dataset_failed(dataset_id, error_code)

    def enqueue_vector_flag_repair(self, document_id: int, version_id: int) -> None:
        """Persist idempotent cleanup work after the authoritative SQLite switch succeeds."""
        return self.spreadsheet_repo.enqueue_vector_flag_repair(document_id, version_id)

    def list_vector_flag_repairs(self) -> list[dict[str, int]]:
        return self.spreadsheet_repo.list_vector_flag_repairs()

    def get_structured_dataset_for_version(self, version_id: int) -> dict[str, Any] | None:
        """Return metadata for exactly one document version, never a current-version fallback."""
        return self.spreadsheet_repo.get_structured_dataset_for_version(version_id)

    def list_current_ready_spreadsheet_datasets(self, kb_id: str) -> list[dict[str, Any]]:
        """Return ready datasets attached to current documents in one knowledge base."""
        return self.spreadsheet_repo.list_current_ready_spreadsheet_datasets(kb_id)

    def list_structured_dataset_schema(self, dataset_id: int) -> list[dict[str, Any]]:
        return self.spreadsheet_repo.list_structured_dataset_schema(dataset_id)

    def replace_structured_dataset_schema(self, dataset_id: int, sheets) -> None:
        """Rewrite the mirrored sheet/column manifest of one dataset."""
        return self.spreadsheet_repo.replace_structured_dataset_schema(dataset_id, sheets)

    def recount_structured_dataset(self, dataset_id: int) -> None:
        """Recompute a dataset's sheet/row/column counters from its mirror rows."""
        return self.spreadsheet_repo.recount_structured_dataset(dataset_id)

    def record_structured_query_run(self, user_id, dataset_ids, plan, status, error_code, rows_scanned, duration_ms) -> int:
        return self.spreadsheet_repo.record_structured_query_run(user_id, dataset_ids, plan, status, error_code, rows_scanned, duration_ms)

    def reserve_spreadsheet_edit(self, *, document_id: int, base_version_id: int, actor_user_id: int, original_filename: str, staged_path: str, content_hash: str, operation: str, reason: str, restored_from_version_id: int | None, changes: tuple[dict[str, Any], ...], ip_address: str='', user_agent: str='', risk_flags: tuple[str, ...]=(), structure_change: dict[str, Any] | None=None) -> tuple[int, int]:
        """Atomically reserve a spreadsheet version and its immutable audit rows."""
        return self.spreadsheet_repo.reserve_spreadsheet_edit(document_id=document_id, base_version_id=base_version_id, actor_user_id=actor_user_id, original_filename=original_filename, staged_path=staged_path, content_hash=content_hash, operation=operation, reason=reason, restored_from_version_id=restored_from_version_id, changes=changes, ip_address=ip_address, user_agent=user_agent, risk_flags=risk_flags, structure_change=structure_change)

    def reserve_spreadsheet_structure_edit(self, *, document_id: int, base_version_id: int, actor_user_id: int, original_filename: str, staged_path: str, content_hash: str, reason: str, structure_change: dict[str, Any], ip_address: str='', user_agent: str='', risk_flags: tuple[str, ...]=()) -> tuple[int, int]:
        """Atomically reserve an immutable version and one structure audit row."""
        return self.spreadsheet_repo.reserve_spreadsheet_structure_edit(document_id=document_id, base_version_id=base_version_id, actor_user_id=actor_user_id, original_filename=original_filename, staged_path=staged_path, content_hash=content_hash, reason=reason, structure_change=structure_change, ip_address=ip_address, user_agent=user_agent, risk_flags=risk_flags)

    def count_recent_spreadsheet_edits(self, actor_user_id: int, document_id: int, since: float) -> int:
        return self.spreadsheet_repo.count_recent_spreadsheet_edits(actor_user_id, document_id, since)

    def record_rejected_spreadsheet_edit(self, *, document_id: int, base_version_id: int, actor_user_id: int, operation: str, document_name_snapshot: str, reason: str, error_code: str, risk_flags: tuple[str, ...], ip_address: str='', user_agent: str='') -> int:
        """Persist a value-free audit record for a server-rejected edit attempt."""
        return self.spreadsheet_repo.record_rejected_spreadsheet_edit(document_id=document_id, base_version_id=base_version_id, actor_user_id=actor_user_id, operation=operation, document_name_snapshot=document_name_snapshot, reason=reason, error_code=error_code, risk_flags=risk_flags, ip_address=ip_address, user_agent=user_agent)

    def get_spreadsheet_edit_session(self, session_id: int) -> dict[str, Any] | None:
        return self.spreadsheet_repo.get_spreadsheet_edit_session(session_id)

    def get_spreadsheet_edit_session_for_version(self, version_id: int) -> dict[str, Any] | None:
        return self.spreadsheet_repo.get_spreadsheet_edit_session_for_version(version_id)

    def set_spreadsheet_edit_status(self, session_id: int, status: str, *, activated_at: float | None=None, error_code: str='') -> None:
        return self.spreadsheet_repo.set_spreadsheet_edit_status(session_id, status, activated_at=activated_at, error_code=error_code)

    def create_document_version(self, document_id: int, original_filename: str, file_path: str, content_hash: str, uploaded_by: int) -> int:
        """Reserve a staging version row and return its id."""
        return self.doc_repo.create_document_version(document_id, original_filename, file_path, content_hash, uploaded_by)

    def _activate_document_version_in_tx(self, document_id: int, version_id: int, kb_id: str, filename: str) -> None:
        """Promote a staging version inside an already-open transaction."""
        return self.doc_repo._activate_document_version_in_tx(document_id, version_id, kb_id, filename)

    def activate_document_version(self, document_id: int, version_id: int, kb_id: str, filename: str) -> None:
        """Atomically promote a staging version to current."""
        return self.doc_repo.activate_document_version(document_id, version_id, kb_id, filename)

    def get_current_document_version(self, document_id: int) -> dict[str, Any] | None:
        return self.doc_repo.get_current_document_version(document_id)

    def list_document_versions(self, document_id: int) -> list[dict[str, Any]]:
        return self.doc_repo.list_document_versions(document_id)

    def set_version_validity(self, version_id: int, validity_status: str) -> None:
        return self.doc_repo.set_version_validity(version_id, validity_status)

    def get_document_version_by_id(self, version_id: int) -> dict[str, Any] | None:
        return self.doc_repo.get_document_version_by_id(version_id)

    def set_version_file_path(self, version_id: int, file_path: str) -> None:
        return self.doc_repo.set_version_file_path(version_id, file_path)

    def delete_document_version(self, version_id: int) -> None:
        return self.doc_repo.delete_document_version(version_id)

    def set_version_status(self, version_id: int, status: str) -> None:
        return self.doc_repo.set_version_status(version_id, status)

    def list_document_permissions_all(self) -> list[dict[str, Any]]:
        """Return every logical document for startup migration and metrics."""
        return self.doc_repo.list_document_permissions_all()

    def foreign_key_check(self) -> list[tuple]:
        return self.doc_repo.foreign_key_check()

    def enqueue_latest_index_job(self, document_id: int, version_id: int, content_hash: str, not_before: float) -> int:
        """Enqueue the latest index job for a document, cancelling prior active ones."""
        return self.material_repo.enqueue_latest_index_job(document_id, version_id, content_hash, not_before)

    def enqueue_material_job(self, document_id: int, version_id: int) -> int:
        """Enqueue a job immediately (test and maintenance helper)."""
        return self.material_repo.enqueue_material_job(document_id, version_id)

    def get_index_job(self, job_id: int) -> dict[str, Any] | None:
        return self.material_repo.get_index_job(job_id)

    def settle_material_job(self, job_id: int, worker_id: str, status: str, error_code: str='', available_at: float | None=None) -> bool:
        """Finalize a claimed material job and release its lease."""
        return self.material_repo.settle_material_job(job_id, worker_id, status, error_code, available_at)

    def renew_material_job(self, job_id: int, worker_id: str, now: float, lease_seconds: int) -> bool:
        """Extend the current worker's lease before a long job can be reclaimed."""
        return self.material_repo.renew_material_job(job_id, worker_id, now, lease_seconds)

    def claim_material_jobs(self, worker_id: str, now: float, limit: int, lease_seconds: int) -> list[dict[str, Any]]:
        """Claim due, unlocked jobs with one compare-and-set update each."""
        return self.material_repo.claim_material_jobs(worker_id, now, limit, lease_seconds)

    def create_material_archive_manifest(self, container_document_id: int, container_version_id: int, source_path: str, created_by: int, expires_at: float) -> int:
        return self.material_repo.create_material_archive_manifest(container_document_id, container_version_id, source_path, created_by, expires_at)

    def get_material_archive_manifest(self, manifest_id: int) -> dict[str, Any] | None:
        return self.material_repo.get_material_archive_manifest(manifest_id)

    def set_material_archive_manifest_status(self, manifest_id: int, status: str) -> None:
        return self.material_repo.set_material_archive_manifest_status(manifest_id, status)

    def add_material_archive_entry(self, manifest_id: int, relative_path: str, normalized_path: str, child_source_key: str, size_bytes: int, state: str, reason_code: str='') -> int:
        return self.material_repo.add_material_archive_entry(manifest_id, relative_path, normalized_path, child_source_key, size_bytes, state, reason_code)

    def list_material_archive_entries(self, manifest_id: int) -> list[dict[str, Any]]:
        return self.material_repo.list_material_archive_entries(manifest_id)

    def set_material_archive_entry_content(self, entry_id: int, content_hash: str) -> None:
        return self.material_repo.set_material_archive_entry_content(entry_id, content_hash)

    def get_container_child(self, container_document_id: int, relative_path: str) -> dict[str, Any] | None:
        return self.material_repo.get_container_child(container_document_id, relative_path)

    def set_child_container(self, document_id: int, container_document_id: int, relative_path: str) -> None:
        return self.material_repo.set_child_container(document_id, container_document_id, relative_path)

    def list_container_children(self, container_document_id: int) -> list[dict[str, Any]]:
        return self.material_repo.list_container_children(container_document_id)

    def get_archive_import_result(self, manifest_id: int, idempotency_key: str) -> dict[str, Any] | None:
        return self.material_repo.get_archive_import_result(manifest_id, idempotency_key)

    def create_archive_import_result(self, manifest_id: int, idempotency_key: str, container_document_id: int, container_version_id: int, result_json: str) -> int:
        return self.material_repo.create_archive_import_result(manifest_id, idempotency_key, container_document_id, container_version_id, result_json)

    def insert_material_search_fts(self, document_id: int, version_id: int, filename: str, logical_path: str, document_type: str, tags: str, extracted_text: str) -> None:
        return self.material_repo.insert_material_search_fts(document_id, version_id, filename, logical_path, document_type, tags, extracted_text)

    def search_material_search_fts(self, text: str, limit: int=50) -> list[dict[str, Any]]:
        return self.material_repo.search_material_search_fts(text, limit)

    def create_organization_proposal(self, actor_id: int, instruction_hash: str, expires_at: float) -> int:
        return self.material_repo.create_organization_proposal(actor_id, instruction_hash, expires_at)

    def get_organization_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        return self.material_repo.get_organization_proposal(proposal_id)

    def set_organization_proposal_status(self, proposal_id: int, status: str) -> None:
        return self.material_repo.set_organization_proposal_status(proposal_id, status)

    def add_organization_item(self, proposal_id: int, document_id: int, target_path: str, add_tags: str, remove_tags: str, document_type: str, reason: str, confidence: float) -> int:
        return self.material_repo.add_organization_item(proposal_id, document_id, target_path, add_tags, remove_tags, document_type, reason, confidence)

    def list_organization_items(self, proposal_id: int) -> list[dict[str, Any]]:
        return self.material_repo.list_organization_items(proposal_id)

    def set_organization_item_status(self, item_id: int, status: str, reason_code: str='') -> None:
        return self.material_repo.set_organization_item_status(item_id, status, reason_code)

    def get_organization_batch(self, proposal_id: int, idempotency_key: str) -> dict[str, Any] | None:
        return self.material_repo.get_organization_batch(proposal_id, idempotency_key)

    def _create_organization_batch_in_tx(self, proposal_id: int, actor_id: int, idempotency_key: str) -> int:
        """Insert a batch row inside an already-open transaction."""
        return self.material_repo._create_organization_batch_in_tx(proposal_id, actor_id, idempotency_key)

    def create_organization_batch(self, proposal_id: int, actor_id: int, idempotency_key: str) -> int:
        return self.material_repo.create_organization_batch(proposal_id, actor_id, idempotency_key)

    def add_organization_batch_item(self, batch_id: int, document_id: int, before_logical_path: str, after_logical_path: str, before_metadata_version: int, after_metadata_version: int, status: str, reason_code: str) -> int:
        return self.material_repo.add_organization_batch_item(batch_id, document_id, before_logical_path, after_logical_path, before_metadata_version, after_metadata_version, status, reason_code)

    def list_organization_batch_items(self, batch_id: int) -> list[dict[str, Any]]:
        return self.material_repo.list_organization_batch_items(batch_id)

    def get_organization_batch_by_id(self, batch_id: int) -> dict[str, Any] | None:
        return self.material_repo.get_organization_batch_by_id(batch_id)

    def current_version_id(self, document_id: int) -> int | None:
        return self.doc_repo.current_version_id(document_id)

    def get_latest_version_id(self, document_id: int) -> int | None:
        return self.doc_repo.get_latest_version_id(document_id)

    def store_unit_fingerprints(self, document_id: int, version_id: int, fingerprints: dict[str, str]) -> None:
        return self.material_repo.store_unit_fingerprints(document_id, version_id, fingerprints)

    def load_unit_fingerprints(self, document_id: int, version_id: int) -> dict[str, str]:
        return self.material_repo.load_unit_fingerprints(document_id, version_id)

    def is_document_deleted(self, document_id: int) -> bool:
        return self.doc_repo.is_document_deleted(document_id)

    def list_recycled_documents(self) -> list[dict[str, Any]]:
        return self.doc_repo.list_recycled_documents()

    def get_citation_state(self, document_id: int) -> str:
        return self.doc_repo.get_citation_state(document_id)
