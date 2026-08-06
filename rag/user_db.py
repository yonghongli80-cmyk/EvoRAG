"""SQLite-backed user database for conversations, messages, and feedback."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

from config import settings


class VersionConflict(Exception):
    """Raised when a version operation cannot proceed due to state conflict."""


def _validate_password_strength(password: str) -> None:
    """验证密码强度。不符合要求时抛出 ValueError。"""
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    if not any(c.isupper() for c in password):
        raise ValueError("密码需含大写字母")
    if not any(c.islower() for c in password):
        raise ValueError("密码需含小写字母")
    if not any(c.isdigit() for c in password):
        raise ValueError("密码需含数字")


class UserDB:
    """Thread-safe SQLite database for users, conversations, messages, and feedback."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    # ------------------------------------------------------------------
    # Transaction helpers
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self, immediate: bool = False):
        """Own exactly one BEGIN/COMMIT/ROLLBACK.

        Callers must already hold ``self._lock``. Row-mutation helpers
        decorated with ``_in_tx`` never begin, commit, or roll back a
        transaction themselves; compound operations wrap them in one
        public transaction so a failure rolls back all rows together.
        """
        if immediate:
            self._conn.execute("BEGIN IMMEDIATE")
        else:
            self._conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    username    TEXT    NOT NULL UNIQUE,
                    salt        TEXT    NOT NULL,
                    password    TEXT    NOT NULL,
                    created_at  REAL    NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL REFERENCES users(id),
                    title       TEXT    NOT NULL DEFAULT '',
                    created_at  REAL    NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                    role            TEXT    NOT NULL,
                    content         TEXT    NOT NULL,
                    created_at      REAL    NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id  INTEGER NOT NULL REFERENCES chat_messages(id),
                    user_id     INTEGER NOT NULL REFERENCES users(id),
                    value       INTEGER NOT NULL,
                    comment     TEXT    NOT NULL DEFAULT '',
                    created_at  REAL    NOT NULL DEFAULT (strftime('%s','now')),
                    UNIQUE(message_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS kb_metadata (
                    kb_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    overview TEXT DEFAULT '',
                    user_id INTEGER,
                    owner_id INTEGER DEFAULT 0,
                    scope TEXT DEFAULT 'private',
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS kb_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    file_path TEXT,
                    toc TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    chunk_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    last_error TEXT DEFAULT '',
                    added_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(kb_id, filename)
                );

                CREATE TABLE IF NOT EXISTS data_sources (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT    NOT NULL,
                    type            TEXT    NOT NULL,
                    config          TEXT    NOT NULL DEFAULT '{}',
                    status          TEXT    NOT NULL DEFAULT 'inactive',
                    last_synced_at  REAL,
                    created_at      REAL    NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS analysis_cards (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT    NOT NULL,
                    user_id     INTEGER,
                    created_at  TEXT    DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS analysis_questions (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    card_id             INTEGER NOT NULL,
                    question            TEXT    NOT NULL,
                    answer              TEXT    DEFAULT '',
                    source_mode         TEXT    DEFAULT '',
                    source_message_id   INTEGER,
                    created_at          TEXT    DEFAULT (datetime('now')),
                    FOREIGN KEY (card_id) REFERENCES analysis_cards(id)
                );

                CREATE TABLE IF NOT EXISTS document_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_name TEXT NOT NULL,
                    kb_id TEXT NOT NULL,
                    owner_id INTEGER NOT NULL,
                    permission_level INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(doc_name, kb_id)
                );

                CREATE INDEX IF NOT EXISTS idx_doc_permissions_kb ON document_permissions(kb_id);
                CREATE INDEX IF NOT EXISTS idx_doc_permissions_owner ON document_permissions(owner_id);

                CREATE TABLE IF NOT EXISTS document_shares (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    granted_by INTEGER NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(doc_id, user_id),
                    FOREIGN KEY (doc_id) REFERENCES document_permissions(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (granted_by) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_doc_shares_user ON document_shares(user_id);
                CREATE INDEX IF NOT EXISTS idx_doc_shares_doc ON document_shares(doc_id);

                CREATE TABLE IF NOT EXISTS kb_shares (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    permission TEXT DEFAULT 'view',
                    granted_by INTEGER NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(kb_id, user_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_kb_shares_user ON kb_shares(user_id);
                CREATE INDEX IF NOT EXISTS idx_kb_shares_kb ON kb_shares(kb_id);

                CREATE TABLE IF NOT EXISTS document_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    version_no INTEGER NOT NULL,
                    original_filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    uploaded_by INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('staging', 'current', 'historical', 'failed')),
                    validity_status TEXT NOT NULL CHECK(validity_status IN (
                        'valid', 'reported_outdated', 'confirmed_outdated'
                    )) DEFAULT 'valid',
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    superseded_at REAL,
                    UNIQUE(document_id, version_no),
                    FOREIGN KEY(document_id) REFERENCES document_permissions(id) ON DELETE CASCADE,
                    FOREIGN KEY(uploaded_by) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS answer_citations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assistant_message_id INTEGER NOT NULL,
                    document_id INTEGER NOT NULL,
                    document_version_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    section_title TEXT NOT NULL,
                    text_preview TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    is_historical INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    FOREIGN KEY(assistant_message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS knowledge_tasks (
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
                );

                CREATE TABLE IF NOT EXISTS knowledge_task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL,
                    old_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                """
            )
            # Add permission_level column to users if it doesn't exist (idempotent)
            try:
                self._conn.execute("SELECT permission_level FROM users LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN permission_level INTEGER NOT NULL DEFAULT 1"
                )
                self._conn.commit()

            # Add is_admin column to users if it doesn't exist (idempotent)
            try:
                self._conn.execute("SELECT is_admin FROM users LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"
                )
                self._conn.commit()

            # Add is_public column to document_permissions if it doesn't exist (idempotent)
            try:
                self._conn.execute("SELECT is_public FROM document_permissions LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute(
                    "ALTER TABLE document_permissions ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT 0"
                )
                self._conn.commit()

            # Add protected column to document_permissions if it doesn't exist (idempotent)
            try:
                self._conn.execute("SELECT protected FROM document_permissions LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute(
                    "ALTER TABLE document_permissions ADD COLUMN protected BOOLEAN NOT NULL DEFAULT 0"
                )
                self._conn.commit()

            # Add mode column to conversations if it doesn't exist (idempotent)
            try:
                self._conn.execute("SELECT mode FROM conversations LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute(
                    "ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'file'"
                )
                self._conn.commit()
            # Backfill NULL mode values to 'file' (for conversations created before mode column existed)
            self._conn.execute(
                "UPDATE conversations SET mode = 'file' WHERE mode IS NULL"
            )
            # Add summary column to analysis_cards if it doesn't exist (idempotent)
            try:
                self._conn.execute("SELECT summary FROM analysis_cards LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute(
                    "ALTER TABLE analysis_cards ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
                )
            self._conn.commit()

            # Phase 1 migration: add owner_id and scope to kb_metadata if missing
            try:
                self._conn.execute("SELECT owner_id FROM kb_metadata LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE kb_metadata ADD COLUMN owner_id INTEGER DEFAULT 0")
                self._conn.execute("ALTER TABLE kb_metadata ADD COLUMN scope TEXT DEFAULT 'private'")
                # Migrate old data
                self._conn.execute("UPDATE kb_metadata SET owner_id = 0, scope = 'public' WHERE user_id IS NULL")
                self._conn.execute("UPDATE kb_metadata SET owner_id = user_id, scope = 'private' WHERE user_id IS NOT NULL")
                self._conn.commit()

            # Phase 1 migration: add scope to document_permissions if missing
            try:
                self._conn.execute("SELECT scope FROM document_permissions LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE document_permissions ADD COLUMN scope TEXT DEFAULT 'private'")
                # 用 CASE 一次性迁移，优先级：protected > is_public > 默认 private
                self._conn.execute("""
                    UPDATE document_permissions SET scope = CASE
                        WHEN protected = 1 THEN 'public'
                        WHEN is_public = 1 THEN 'public'
                        ELSE 'private'
                    END
                """)
                self._conn.commit()

            # Phase 2 migration: add permission to document_shares if missing
            try:
                self._conn.execute("SELECT permission FROM document_shares LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE document_shares ADD COLUMN permission TEXT DEFAULT 'view'")
                self._conn.commit()

            # Phase 3 migration: add downloadable to document_permissions if missing
            try:
                self._conn.execute("SELECT downloadable FROM document_permissions LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE document_permissions ADD COLUMN downloadable INTEGER DEFAULT 1")
                self._conn.commit()

            # Phase 1a migration: add password_changed_at to users if missing
            try:
                self._conn.execute("SELECT password_changed_at FROM users LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE users ADD COLUMN password_changed_at REAL DEFAULT NULL")
                self._conn.commit()

            # Persist only a bounded diagnostic for retryable KB document work.
            try:
                self._conn.execute("SELECT last_error FROM kb_documents LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE kb_documents ADD COLUMN last_error TEXT DEFAULT ''")
                self._conn.commit()

            # Trusted knowledge loop migrations: version pointers on documents.
            try:
                self._conn.execute("SELECT current_version_id FROM document_permissions LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE document_permissions ADD COLUMN current_version_id INTEGER")
                self._conn.commit()
            try:
                self._conn.execute("SELECT source_type FROM document_permissions LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE document_permissions ADD COLUMN source_type TEXT NOT NULL DEFAULT 'upload'")
                self._conn.commit()
            try:
                self._conn.execute("SELECT source_key FROM document_permissions LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE document_permissions ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
                self._conn.commit()
            try:
                self._conn.execute("SELECT updated_at FROM document_permissions LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE document_permissions ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
                self._conn.commit()

            # Trusted knowledge loop migrations: citation persistence columns.
            try:
                self._conn.execute("SELECT persistence_key FROM chat_messages LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE chat_messages ADD COLUMN persistence_key TEXT")
                self._conn.execute("ALTER TABLE chat_messages ADD COLUMN citations_hash TEXT")
                self._conn.execute("ALTER TABLE chat_messages ADD COLUMN citation_count INTEGER DEFAULT 0")
                self._conn.commit()
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_persistence "
                "ON chat_messages(conversation_id, persistence_key)"
            )

            # Trusted knowledge loop migrations: structured feedback columns.
            for column, ddl in (
                ("kind", "TEXT NOT NULL DEFAULT 'not_useful'"),
                ("question_text", "TEXT NOT NULL DEFAULT ''"),
                ("question_hash", "TEXT NOT NULL DEFAULT ''"),
                ("citation_id", "INTEGER"),
                ("suggested_answer", "TEXT NOT NULL DEFAULT ''"),
                ("status", "TEXT NOT NULL DEFAULT 'submitted'"),
            ):
                try:
                    self._conn.execute(f"SELECT {column} FROM feedback LIMIT 1")
                except sqlite3.OperationalError:
                    self._conn.execute(
                        f"ALTER TABLE feedback ADD COLUMN {column} {ddl}"
                    )
                    self._conn.commit()

            # Trusted knowledge loop migrations: version pointers on kb_documents.
            try:
                self._conn.execute("SELECT document_id FROM kb_documents LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE kb_documents ADD COLUMN document_id INTEGER")
                self._conn.commit()
            try:
                self._conn.execute("SELECT document_version_id FROM kb_documents LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE kb_documents ADD COLUMN document_version_id INTEGER")
                self._conn.commit()

            # Material lifecycle migrations: logical metadata columns.
            for column, ddl in (
                ("logical_path", "TEXT NOT NULL DEFAULT ''"),
                ("document_type", "TEXT NOT NULL DEFAULT ''"),
                ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("subject_user_id", "INTEGER"),
                ("metadata_version", "INTEGER NOT NULL DEFAULT 1"),
                ("deleted_at", "REAL"),
                ("deleted_by", "INTEGER"),
                ("restore_until", "REAL"),
                ("container_document_id", "INTEGER"),
                ("container_relative_path", "TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    self._conn.execute(f"SELECT {column} FROM document_permissions LIMIT 1")
                except sqlite3.OperationalError:
                    self._conn.execute(
                        f"ALTER TABLE document_permissions ADD COLUMN {column} {ddl}"
                    )
                    self._conn.commit()

    # ------------------------------------------------------------------
    # User Search (Phase 2)
    # ------------------------------------------------------------------

    def search_users(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """按用户名搜索用户。返回 [{id, username}]，最多 limit 条。"""
        # 轶义 LIKE 通配符
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username FROM users WHERE username LIKE ? ESCAPE '\\' LIMIT ?",
                (f"%{escaped}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Password Management (Phase 1a)
    # ------------------------------------------------------------------

    def change_password(self, user_id: int, old_password: str, new_password: str) -> None:
        """修改密码。验证旧密码、新密码强度、新旧密码不同。"""
        import time
        from rag.auth import verify_password, hash_password

        _validate_password_strength(new_password)
        if old_password == new_password:
            raise ValueError("新密码不能与旧密码相同")

        with self._lock:
            row = self._conn.execute(
                "SELECT id, password FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not row:
                raise ValueError("用户不存在")
            if not verify_password(old_password, row["password"]):
                raise ValueError("旧密码错误")
            new_hashed = hash_password(new_password)
            self._conn.execute(
                "UPDATE users SET password = ?, password_changed_at = ? WHERE id = ?",
                (new_hashed, time.time(), user_id),
            )
            self._conn.commit()

    def reset_password(self, user_id: int, new_password: str) -> None:
        """重置密码（不验证旧密码）。仅 admin 调用。"""
        import time
        from rag.auth import hash_password

        _validate_password_strength(new_password)

        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not row:
                raise ValueError("用户不存在")
            new_hashed = hash_password(new_password)
            self._conn.execute(
                "UPDATE users SET password = ?, password_changed_at = ? WHERE id = ?",
                (new_hashed, time.time(), user_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # KB Metadata (Phase 1: owner/scope)
    # ------------------------------------------------------------------

    def create_kb_metadata(self, kb_id: str, name: str, owner_id: int = 0, scope: str = "private") -> None:
        """创建 KB 元数据（仅新 KB 调用，不覆盖已有数据）。"""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO kb_metadata (kb_id, name, owner_id, scope) VALUES (?, ?, ?, ?)",
                    (kb_id, name, owner_id, scope),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                pass  # 已存在，忽略（Qdrant KB 已创建但 metadata 重复插入的情况）

    def get_kb_metadata(self, kb_id: str) -> dict[str, Any] | None:
        """获取 KB 元数据。不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT kb_id, name, owner_id, scope, created_at FROM kb_metadata WHERE kb_id = ?",
                (kb_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    # ------------------------------------------------------------------
    # KB Documents (durable source-file lifecycle)
    # ------------------------------------------------------------------

    def create_kb_document_pending(self, kb_id: str, filename: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO kb_documents (kb_id, filename, status) VALUES (?, ?, 'pending')",
                (kb_id, filename),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_kb_document(
        self, document_id: int, file_path: str, toc: str, summary: str, chunk_count: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE kb_documents SET file_path = ?, toc = ?, summary = ?, "
                "chunk_count = ?, status = 'indexed', last_error = '' WHERE id = ?",
                (file_path, toc, summary, chunk_count, document_id),
            )
            self._conn.commit()

    def set_kb_document_state(
        self, document_id: int, status: str, last_error: str = ""
    ) -> None:
        if status not in {"pending", "indexed", "deleting", "delete_failed"}:
            raise ValueError("invalid kb document status")
        with self._lock:
            self._conn.execute(
                "UPDATE kb_documents SET status = ?, last_error = ? WHERE id = ?",
                (status, last_error[:500], document_id),
            )
            self._conn.commit()

    def get_kb_document(self, kb_id: str, filename: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, kb_id, filename, file_path, toc, summary, chunk_count, status "
                "FROM kb_documents WHERE kb_id = ? AND filename = ?",
                (kb_id, filename),
            ).fetchone()
        return dict(row) if row else None

    def list_kb_documents(self, kb_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kb_id, filename, file_path, toc, summary, chunk_count, status "
                "FROM kb_documents WHERE kb_id = ? ORDER BY id",
                (kb_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_kb_document(self, document_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kb_documents WHERE id = ?", (document_id,))
            self._conn.commit()

    def update_kb_scope(self, kb_id: str, scope: str) -> None:
        """更新 KB 的 scope 字段。scope 必须是 'private' 或 'public'。

        切换到 private 或 public 时，清除该 KB 的所有 shares（shared 模式才需要 shares）。
        """
        if scope not in ("private", "public"):
            raise ValueError(f"无效的 scope: {scope}，必须是 'private' 或 'public'")
        with self._lock:
            self._conn.execute(
                "UPDATE kb_metadata SET scope = ? WHERE kb_id = ?",
                (scope, kb_id),
            )
            # 切换到非 shared 模式时清除 shares
            self._conn.execute(
                "DELETE FROM kb_shares WHERE kb_id = ?",
                (kb_id,),
            )
            self._conn.commit()

    def update_kb_overview(self, kb_id: str, overview: str) -> bool:
        """Update an existing knowledge base overview through the shared connection."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE kb_metadata SET overview = ? WHERE kb_id = ?",
                (overview, kb_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_kb_document_toc(self, kb_id: str, filename: str, toc: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE kb_documents SET toc = ? WHERE kb_id = ? AND filename = ?",
                (toc, kb_id, filename),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_kb_document_summary(self, kb_id: str, filename: str, summary: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE kb_documents SET summary = ? WHERE kb_id = ? AND filename = ?",
                (summary, kb_id, filename),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_kb_metadata_by_names(self, kb_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """批量查询 KB 元数据。返回 {kb_id: meta_dict_or_None}。"""
        if not kb_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" for _ in kb_ids)
            rows = self._conn.execute(
                f"SELECT kb_id, name, owner_id, scope, created_at FROM kb_metadata WHERE kb_id IN ({placeholders})",
                kb_ids,
            ).fetchall()
        meta_map = {r["kb_id"]: dict(r) for r in rows}
        return {kid: meta_map.get(kid) for kid in kb_ids}

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def create_user(self, username: str, password: str) -> int:
        """Create a new user.  Raises ``ValueError`` if the username is taken."""
        from rag.auth import hash_password

        hashed = hash_password(password)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users (username, salt, password) VALUES (?, ?, ?)",
                    (username, "", hashed),
                )
                self._conn.commit()
                return cur.lastrowid  # type: ignore[return-value]
            except sqlite3.IntegrityError:
                raise ValueError(f"Username '{username}' already exists")

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """Return user dict on success, ``None`` on failure."""
        from rag.auth import verify_password

        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, password FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        if not verify_password(password, row["password"]):
            return None
        return {"id": row["id"], "username": row["username"]}

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Return user dict or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, is_admin, password_changed_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
            "password_changed_at": row["password_changed_at"],
        }

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def create_conversation(self, user_id: int | str, title: str = "", mode: str = "file") -> int:
        """Create a conversation and return its id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO conversations (user_id, title, mode) VALUES (?, ?, ?)",
                (int(user_id), title, mode),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_conversations(self, user_id: int | str, mode: str | None = None) -> list[dict[str, Any]]:
        """Return all conversations for *user_id*, newest first.

        If *mode* is given, filter by that mode.
        """
        uid = int(user_id)
        with self._lock:
            if mode is not None:
                rows = self._conn.execute(
                    "SELECT id, user_id, title, mode, created_at "
                    "FROM conversations WHERE user_id = ? AND mode = ? "
                    "ORDER BY created_at DESC",
                    (uid, mode),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, user_id, title, mode, created_at "
                    "FROM conversations WHERE user_id = ? ORDER BY created_at DESC",
                    (uid,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, conversation_id: int, user_id: int | str) -> dict[str, Any] | None:
        """获取对话（仅 owner 可见）。"""
        uid = int(user_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT id, user_id, title, mode, created_at FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, uid),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def delete_conversation(self, conversation_id: int, user_id: int | str) -> bool:
        """Delete a conversation (only if owned by user_id). Returns True if deleted."""
        uid = int(user_id)
        with self._lock:
            self._conn.execute(
                "DELETE FROM chat_messages WHERE conversation_id = ? "
                "AND conversation_id IN (SELECT id FROM conversations WHERE id = ? AND user_id = ?)",
                (conversation_id, conversation_id, uid),
            )
            cur = self._conn.execute(
                "DELETE FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, uid),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_conversation_title(self, conversation_id: int, user_id: int | str, title: str) -> bool:
        """更新对话标题（仅 owner 可操作）。返回是否成功。"""
        uid = int(user_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
                (title, conversation_id, uid),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def search_conversations(self, user_id: int | str, q: str, page: int = 1, size: int = 20) -> list[dict[str, Any]]:
        """搜索对话（标题 + 消息内容）。返回匹配的对话列表。"""
        uid = int(user_id)
        offset = (page - 1) * size
        # 转义 LIKE 通配符
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self._lock:
            rows = self._conn.execute("""
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

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def add_message(self, conversation_id: int, role: str, content: str) -> int:
        """Append a message to a conversation and return its id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO chat_messages (conversation_id, role, content) VALUES (?, ?, ?)",
                (conversation_id, role, content),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_messages(self, conversation_id: int, user_id: int | str | None = None) -> list[dict[str, Any]]:
        """Return all messages in a conversation.

        When ``user_id`` is given, ownership is verified; otherwise all
        messages for the conversation are returned.
        """
        with self._lock:
            if user_id is not None:
                rows = self._conn.execute(
                    "SELECT m.id, m.conversation_id, m.role, m.content, m.created_at "
                    "FROM chat_messages m "
                    "JOIN conversations c ON m.conversation_id = c.id "
                    "WHERE m.conversation_id = ? AND c.user_id = ? ORDER BY m.created_at",
                    (conversation_id, int(user_id)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT m.id, m.conversation_id, m.role, m.content, m.created_at "
                    "FROM chat_messages m WHERE m.conversation_id = ? ORDER BY m.created_at",
                    (conversation_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    def update_message(self, message_id: int, new_content: str, user_id: int = None):
        """更新消息内容（用于重新生成）。可选 user_id 校验所有权。"""
        with self._lock:
            if user_id is not None:
                # Verify the message belongs to a conversation owned by this user
                self._conn.execute(
                    "UPDATE chat_messages SET content = ? WHERE id = ? "
                    "AND conversation_id IN (SELECT id FROM conversations WHERE user_id = ?)",
                    (new_content, message_id, user_id),
                )
            else:
                self._conn.execute(
                    "UPDATE chat_messages SET content = ? WHERE id = ?",
                    (new_content, message_id),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def add_feedback(
        self,
        message_id: int,
        user_id: int | str,
        value: int,
        comment: str = "",
    ) -> int:
        """Record user feedback on a message and return feedback id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR REPLACE INTO feedback (message_id, user_id, value, comment) VALUES (?, ?, ?, ?)",
                (message_id, int(user_id), value, comment),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def message_belongs_to_user(self, message_id: int, user_id: int | str) -> bool:
        """Check if a message belongs to a conversation owned by user_id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM chat_messages m "
                "JOIN conversations c ON m.conversation_id = c.id "
                "WHERE m.id = ? AND c.user_id = ?",
                (message_id, int(user_id)),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Structured knowledge feedback
    # ------------------------------------------------------------------

    def get_message_question(self, assistant_message_id: int) -> str:
        """Return the user question preceding an assistant message, if any."""
        with self._lock:
            row = self._conn.execute(
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
        with self._lock, self.transaction(immediate=True):
            self._conn.execute(
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
            row = self._conn.execute(
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
        with self._lock:
            total_row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM feedback WHERE user_id = ?", (uid,)
            ).fetchone()
            rows = self._conn.execute(
                "SELECT f.id, f.message_id, f.kind, f.question_text, f.question_hash, "
                "f.citation_id, f.comment, f.suggested_answer, f.status, f.created_at, "
                "u.username AS submitter_name "
                "FROM feedback f JOIN users u ON u.id = f.user_id "
                "WHERE f.user_id = ? ORDER BY f.created_at DESC LIMIT ? OFFSET ?",
                (uid, page_size, offset),
            ).fetchall()
        return [dict(r) for r in rows], int(total_row["cnt"])

    # ------------------------------------------------------------------
    # Knowledge correction tasks
    # ------------------------------------------------------------------

    def get_feedback(self, feedback_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, message_id, user_id, kind, question_text, question_hash, "
                "citation_id, suggested_answer, comment, status, created_at "
                "FROM feedback WHERE id = ?",
                (feedback_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_answer_citation_by_id(self, citation_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM answer_citations WHERE id = ?", (citation_id,)
            ).fetchone()
        return dict(row) if row else None

    def count_distinct_feedback_users(self, question_hash: str, days: int) -> int:
        cutoff = time.time() - days * 86400
        with self._lock:
            row = self._conn.execute(
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
        now = time.time()
        due_at = now + settings.knowledge_task_due_days * 86400
        with self._lock, self.transaction(immediate=True):
            row = self._conn.execute(
                "SELECT id FROM knowledge_tasks WHERE active_key = ? "
                "AND status IN ('open', 'in_progress')",
                (active_key,),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE knowledge_tasks SET report_count = ?, updated_at = ? "
                    "WHERE id = ?",
                    (report_count, now, row["id"]),
                )
                return int(row["id"])
            cur = self._conn.execute(
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

    def get_knowledge_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_tasks_for_user(
        self, user_id: int | str, page: int = 1, page_size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        uid = int(user_id)
        offset = (page - 1) * page_size
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks WHERE assigned_to = ?",
                (uid,),
            ).fetchone()
            rows = self._conn.execute(
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
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS cnt FROM knowledge_tasks {where}", params
            ).fetchone()
            rows = self._conn.execute(
                f"SELECT * FROM knowledge_tasks {where} ORDER BY due_at ASC LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall()
        return [dict(r) for r in rows], int(total["cnt"])

    def transition_knowledge_task(
        self,
        task_id: int,
        actor_id: int,
        new_status: str,
        note: str,
        expected_updated_at: float,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self.transaction(immediate=True):
            row = self._conn.execute(
                "SELECT status, updated_at FROM knowledge_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            old_status = row["status"]
            resolved_at = now if new_status in {"resolved", "dismissed", "confirmed_expired"} else None
            cur = self._conn.execute(
                "UPDATE knowledge_tasks SET status = ?, active_key = NULL, "
                "resolution_note = ?, updated_at = ?, resolved_at = ? "
                "WHERE id = ? AND updated_at = ?",
                (new_status, note, now, resolved_at, task_id, expected_updated_at),
            )
            if cur.rowcount == 0:
                raise VersionConflict("task_state_conflict")
            self._conn.execute(
                "INSERT INTO knowledge_task_events "
                "(task_id, actor_id, old_status, new_status, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, actor_id, old_status, new_status, note, now),
            )
        return self.get_knowledge_task(task_id)

    def reassign_knowledge_task(self, task_id: int, assignee_id: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE knowledge_tasks SET assigned_to = ? WHERE id = ?",
                (assignee_id, task_id),
            )
            self._conn.commit()

    def set_document_owner(self, document_id: int, owner_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE document_permissions SET owner_id = ? WHERE id = ?",
                (owner_id, document_id),
            )
            self._conn.commit()

    def list_task_reporters(self, task_id: int) -> list[str]:
        task = self.get_knowledge_task(task_id)
        if task is None:
            return []
        cutoff = task["created_at"] - settings.feedback_cluster_days * 86400
        with self._lock:
            if task["task_type"] == "outdated":
                rows = self._conn.execute(
                    "SELECT DISTINCT u.username FROM feedback f "
                    "JOIN answer_citations c ON c.id = f.citation_id "
                    "JOIN users u ON u.id = f.user_id "
                    "WHERE c.document_id = ? AND f.kind = 'outdated' AND f.created_at >= ?",
                    (task["document_id"], cutoff),
                ).fetchall()
            else:
                prefix = "not_useful:"
                if task["cluster_key"].startswith(prefix):
                    qh = task["cluster_key"].split(":")[1]
                else:
                    qh = None
                if qh is None:
                    return []
                rows = self._conn.execute(
                    "SELECT DISTINCT u.username FROM feedback f JOIN users u ON u.id = f.user_id "
                    "WHERE f.kind = 'not_useful' AND f.question_hash = ? AND f.created_at >= ?",
                    (qh, cutoff),
                ).fetchall()
        return [r["username"] for r in rows]

    def task_summary(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            open_count = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks WHERE status = 'open'"
            ).fetchone()["cnt"]
            overdue_count = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks "
                "WHERE status IN ('open', 'in_progress') AND due_at < ?",
                (now,),
            ).fetchone()["cnt"]
            outdated_count = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks WHERE task_type = 'outdated' "
                "AND status IN ('open', 'in_progress')"
            ).fetchone()["cnt"]
            not_useful_count = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_tasks WHERE task_type = 'not_useful' "
                "AND status IN ('open', 'in_progress')"
            ).fetchone()["cnt"]
            stale_cutoff = now - settings.knowledge_stale_days * 86400
            stale_count = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM document_permissions "
                "WHERE current_version_id IS NOT NULL AND updated_at < ?",
                (stale_cutoff,),
            ).fetchone()["cnt"]
        return {
            "open": int(open_count),
            "overdue": int(overdue_count),
            "outdated": int(outdated_count),
            "not_useful": int(not_useful_count),
            "stale_knowledge_bases": int(stale_count),
        }

    # ------------------------------------------------------------------
    # Assistant answers with citations
    # ------------------------------------------------------------------

    def _insert_message_in_tx(
        self,
        conversation_id: int,
        role: str,
        content: str,
        persistence_key: str | None = None,
        citations_hash: str | None = None,
        citation_count: int = 0,
    ) -> int:
        """Insert one message row inside an already-open transaction."""
        cur = self._conn.execute(
            "INSERT INTO chat_messages "
            "(conversation_id, role, content, persistence_key, citations_hash, citation_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, persistence_key, citations_hash, citation_count),
        )
        return int(cur.lastrowid)

    def _insert_answer_citations_in_tx(self, assistant_message_id: int, citations: list) -> None:
        """Insert citation rows inside an already-open transaction."""
        for citation in citations:
            self._conn.execute(
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
    ) -> int:
        """Persist one assistant answer and its citations atomically.

        Retrying with the same ``persistence_key`` returns the existing
        message only when content, citation hash, and count all match;
        otherwise a :class:`VersionConflict` is raised instead of silently
        storing different content.
        """
        citations_hash = self._citations_hash(citations)
        with self._lock, self.transaction(immediate=True):
            row = self._conn.execute(
                "SELECT id, content, citations_hash, citation_count FROM chat_messages "
                "WHERE conversation_id = ? AND persistence_key = ?",
                (conversation_id, persistence_key),
            ).fetchone()
            if row is not None:
                if (
                    row["content"] == answer
                    and row["citations_hash"] == citations_hash
                    and row["citation_count"] == len(citations)
                ):
                    return int(row["id"])
                raise VersionConflict("answer persistence idempotency conflict")
            message_id = self._insert_message_in_tx(
                conversation_id,
                "assistant",
                answer,
                persistence_key,
                citations_hash,
                len(citations),
            )
            self._insert_answer_citations_in_tx(message_id, citations)
            return message_id

    @staticmethod
    def _citations_hash(citations: list) -> str:
        import json as _json

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
        digest = hashlib.sha256(_json.dumps(rows, ensure_ascii=False).encode("utf-8"))
        return digest.hexdigest()

    def get_answer_citations(self, assistant_message_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.id, c.document_id, c.document_version_id, c.chunk_index, "
                "c.section_title, c.text_preview, c.rank, c.is_historical, c.created_at, "
                "v.version_no, v.original_filename, v.created_at AS version_created_at "
                "FROM answer_citations c "
                "LEFT JOIN document_versions v ON v.id = c.document_version_id "
                "WHERE c.assistant_message_id = ? ORDER BY c.rank",
                (assistant_message_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Data Sources
    # ------------------------------------------------------------------

    def create_data_source(self, name: str, source_type: str, config: str) -> int:
        """Create a data source and return its id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO data_sources (name, type, config) VALUES (?, ?, ?)",
                (name, source_type, config),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_data_sources(self) -> list[dict[str, Any]]:
        """Return all data sources, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, type, config, status, last_synced_at, created_at "
                "FROM data_sources ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_data_source(self, source_id: int) -> dict[str, Any] | None:
        """Return a single data source by id, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, type, config, status, last_synced_at, created_at "
                "FROM data_sources WHERE id = ?",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_data_source(self, source_id: int) -> bool:
        """Delete a data source.  Returns True if deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM data_sources WHERE id = ?",
                (source_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_data_source_synced(self, source_id: int) -> None:
        """Update the last_synced_at timestamp to now."""
        import time

        with self._lock:
            self._conn.execute(
                "UPDATE data_sources SET last_synced_at = ? WHERE id = ?",
                (time.time(), source_id),
            )
            self._conn.commit()

    def update_data_source_status(self, source_id: int, status: str) -> None:
        """Update the status of a data source."""
        with self._lock:
            self._conn.execute(
                "UPDATE data_sources SET status = ? WHERE id = ?",
                (status, source_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Analysis Cards
    # ------------------------------------------------------------------

    def create_card(self, user_id: int | str, name: str) -> int:
        """Create an analysis card and return its id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO analysis_cards (name, user_id) VALUES (?, ?)",
                (name, int(user_id)),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_cards(self, user_id: int | str) -> list[dict[str, Any]]:
        """Return all analysis cards for *user_id*, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, user_id, created_at FROM analysis_cards "
                "WHERE user_id = ? ORDER BY created_at DESC",
                (int(user_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_card(self, card_id: int, user_id: int | str) -> bool:
        """Delete an analysis card and its questions. Returns True if deleted.

        Only deletes if the card belongs to *user_id*.
        """
        uid = int(user_id)
        with self._lock:
            self._conn.execute(
                "DELETE FROM analysis_questions WHERE card_id = ? "
                "AND card_id IN (SELECT id FROM analysis_cards WHERE id = ? AND user_id = ?)",
                (card_id, card_id, uid),
            )
            cur = self._conn.execute(
                "DELETE FROM analysis_cards WHERE id = ? AND user_id = ?",
                (card_id, uid),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def rename_card(self, card_id: int, name: str, user_id: int | str) -> bool:
        """Rename an analysis card. Returns True if updated.

        Only renames if the card belongs to *user_id*.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE analysis_cards SET name = ? WHERE id = ? AND user_id = ?",
                (name, card_id, int(user_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_card_summary(self, card_id: int) -> str:
        """Return the summary text for a card, empty string if none."""
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM analysis_cards WHERE id = ?",
                (card_id,),
            ).fetchone()
        return row["summary"] if row else ""

    def update_card_summary(self, card_id: int, summary: str, user_id: int | str) -> bool:
        """Update the summary for a card. Returns True if updated.

        Only updates if the card belongs to *user_id*.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE analysis_cards SET summary = ? WHERE id = ? AND user_id = ?",
                (summary, card_id, int(user_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Analysis Questions
    # ------------------------------------------------------------------

    def add_question(
        self,
        card_id: int,
        question: str,
        answer: str = "",
        source_mode: str = "",
        source_message_id: int | None = None,
        user_id: int | str | None = None,
    ) -> int | None:
        """Add a question to a card and return its id.

        If *user_id* is provided, only adds if the card belongs to that user.
        Returns ``None`` if the card does not belong to the user.
        """
        with self._lock:
            if user_id is not None:
                # Verify card ownership
                row = self._conn.execute(
                    "SELECT 1 FROM analysis_cards WHERE id = ? AND user_id = ?",
                    (card_id, int(user_id)),
                ).fetchone()
                if row is None:
                    return None
            cur = self._conn.execute(
                "INSERT INTO analysis_questions "
                "(card_id, question, answer, source_mode, source_message_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (card_id, question, answer, source_mode, source_message_id),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_questions(self, card_id: int) -> list[dict[str, Any]]:
        """Return all questions for a card, ordered by created_at."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, card_id, question, answer, source_mode, "
                "source_message_id, created_at "
                "FROM analysis_questions WHERE card_id = ? ORDER BY created_at",
                (card_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_question(self, question_id: int, user_id: int | str) -> bool:
        """Delete a question. Returns True if deleted.

        Only deletes if the question's card belongs to *user_id*.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM analysis_questions WHERE id = ? "
                "AND card_id IN (SELECT id FROM analysis_cards WHERE user_id = ?)",
                (question_id, int(user_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    def set_user_admin(self, user_id: int, is_admin: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET is_admin = ? WHERE id = ?",
                (int(is_admin), user_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Document Permissions
    # ------------------------------------------------------------------

    def create_document_permission(self, doc_name: str, kb_id: str, owner_id: int, permission_level: int = 1, is_public: bool = False, protected: bool = False, scope: str = None) -> int:
        """创建文档权限记录。scope 不传时根据 is_public 自动推断。"""
        if scope is None:
            scope = "public" if is_public else "private"
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO document_permissions (doc_name, kb_id, owner_id, permission_level, is_public, protected, scope) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doc_name, kb_id, owner_id, permission_level, int(is_public), int(protected), scope),
            )
            self._conn.commit()
            return cur.lastrowid

    _DOCUMENT_PERMISSION_COLUMNS = (
        "id, doc_name, kb_id, owner_id, permission_level, is_public, protected, scope, "
        "downloadable, current_version_id, source_type, source_key, updated_at, created_at, "
        "logical_path, document_type, tags_json, subject_user_id, metadata_version, "
        "deleted_at, deleted_by, restore_until, container_document_id, container_relative_path"
    )

    def get_document_permission(self, doc_name: str, kb_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._DOCUMENT_PERMISSION_COLUMNS} "
                "FROM document_permissions WHERE doc_name = ? AND kb_id = ?",
                (doc_name, kb_id),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["is_public"] = bool(d.get("is_public", 0))
        d["protected"] = bool(d.get("protected", 0))
        d["downloadable"] = bool(d.get("downloadable", 1))
        return d

    def get_document_permission_by_id(self, doc_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._DOCUMENT_PERMISSION_COLUMNS} "
                "FROM document_permissions WHERE id = ?",
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["is_public"] = bool(d.get("is_public", 0))
        d["protected"] = bool(d.get("protected", 0))
        d["downloadable"] = bool(d.get("downloadable", 1))
        return d

    def toggle_document_visibility(self, doc_id: int) -> bool:
        """切换文档公开/私有状态，返回新的 is_public 值。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT is_public FROM document_permissions WHERE id = ?", (doc_id,)
            ).fetchone()
            if not row:
                return False
            new_val = 0 if row["is_public"] else 1
            self._conn.execute(
                "UPDATE document_permissions SET is_public = ? WHERE id = ?",
                (new_val, doc_id),
            )
            self._conn.commit()
            return bool(new_val)

    def delete_document_permission(self, doc_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM document_permissions WHERE id = ?", (doc_id,))
            self._conn.commit()

    def get_accessible_doc_names(self, kb_id: str, user_id: int, user_level: int = 1) -> list[str]:
        """返回用户在指定知识库中有权查看的文档名列表。

        规则：owner / scope='public' / 被共享 的文档可见。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT dp.doc_name FROM document_permissions dp "
                "WHERE dp.kb_id = ? AND (dp.owner_id = ? OR dp.scope = 'public' "
                "OR dp.id IN (SELECT doc_id FROM document_shares WHERE user_id = ?))",
                (kb_id, user_id, user_id),
            ).fetchall()
        return [r["doc_name"] for r in rows]

    def get_document_permissions_by_names(self, doc_names: list[str], kb_id: str) -> dict[str, dict[str, Any] | None]:
        """批量查询多个文档的权限记录。返回 {doc_name: perm_dict_or_None}。"""
        if not doc_names:
            return {}
        with self._lock:
            placeholders = ",".join("?" for _ in doc_names)
            rows = self._conn.execute(
                f"SELECT id, doc_name, kb_id, owner_id, permission_level, is_public, protected, scope, created_at "
                f"FROM document_permissions WHERE doc_name IN ({placeholders}) AND kb_id = ?",
                (*doc_names, kb_id),
            ).fetchall()
        perm_map = {}
        for r in rows:
            d = dict(r)
            d["is_public"] = bool(d.get("is_public", 0))
            d["protected"] = bool(d.get("protected", 0))
            perm_map[d["doc_name"]] = d
        return {name: perm_map.get(name) for name in doc_names}

    def get_document_permissions_by_ids(self, doc_ids: list[int]) -> dict[int, dict[str, Any]]:
        """批量查询多个权限记录（按 ID）。返回 {doc_id: perm_dict}。"""
        if not doc_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" for _ in doc_ids)
            rows = self._conn.execute(
                f"SELECT id, doc_name, kb_id, owner_id, permission_level, is_public, protected, created_at "
                f"FROM document_permissions WHERE id IN ({placeholders})",
                doc_ids,
            ).fetchall()
        result = {}
        for r in rows:
            d = dict(r)
            d["is_public"] = bool(d.get("is_public", 0))
            d["protected"] = bool(d.get("protected", 0))
            result[d["id"]] = d
        return result

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """按用户名查询用户。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, is_admin FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
        }

    # ------------------------------------------------------------------
    # Document Shares
    # ------------------------------------------------------------------

    def share_document(self, doc_id: int, user_id: int, granted_by: int, permission: str = "view") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO document_shares (doc_id, user_id, granted_by, permission) VALUES (?, ?, ?, ?)",
                (doc_id, user_id, granted_by, permission),
            )
            self._conn.execute(
                "UPDATE document_permissions SET scope = 'shared', is_public = 0 "
                "WHERE id = ? AND scope = 'private'",
                (doc_id,),
            )
            self._conn.commit()
            return cur.lastrowid

    def unshare_document(self, doc_id: int, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM document_shares WHERE doc_id = ? AND user_id = ?",
                (doc_id, user_id),
            )
            remaining = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM document_shares WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if remaining["cnt"] == 0:
                self._conn.execute(
                    "UPDATE document_permissions SET scope = 'private' "
                    "WHERE id = ? AND scope = 'shared'",
                    (doc_id,),
                )
            self._conn.commit()

    def is_document_shared(self, doc_id: int, user_id: int, permission: str = None) -> bool:
        """检查文档是否共享给指定用户。permission=None 表示任意权限，'view' 或 'edit' 表示特定权限。"""
        with self._lock:
            if permission:
                row = self._conn.execute(
                    "SELECT 1 FROM document_shares WHERE doc_id = ? AND user_id = ? AND permission = ?",
                    (doc_id, user_id, permission),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM document_shares WHERE doc_id = ? AND user_id = ?",
                    (doc_id, user_id),
                ).fetchone()
        return row is not None

    def is_kb_shared(self, kb_id: str, user_id: int, permission: str = None) -> bool:
        """检查知识库是否共享给指定用户。permission=None 表示任意权限，'view' 或 'edit' 表示特定权限。"""
        with self._lock:
            if permission:
                row = self._conn.execute(
                    "SELECT 1 FROM kb_shares WHERE kb_id = ? AND user_id = ? AND permission = ?",
                    (kb_id, user_id, permission),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM kb_shares WHERE kb_id = ? AND user_id = ?",
                    (kb_id, user_id),
                ).fetchone()
        return row is not None

    def list_shared_users(self, doc_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.id, u.username, ds.permission, ds.granted_by, ds.created_at AS granted_at "
                "FROM document_shares ds JOIN users u ON ds.user_id = u.id "
                "WHERE ds.doc_id = ? ORDER BY ds.created_at",
                (doc_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_shared_doc_ids_for_user(self, user_id: int, doc_ids: list[int]) -> set[int]:
        """批量查询用户被共享的 doc_id 集合。返回指定 doc_ids 中被共享的子集。"""
        if not doc_ids:
            return set()
        with self._lock:
            placeholders = ",".join("?" for _ in doc_ids)
            rows = self._conn.execute(
                f"SELECT doc_id FROM document_shares WHERE user_id = ? AND doc_id IN ({placeholders})",
                (user_id, *doc_ids),
            ).fetchall()
        return {r["doc_id"] for r in rows}

    # ------------------------------------------------------------------
    # KB Shares (Phase 2)
    # ------------------------------------------------------------------

    def share_kb(self, kb_id: str, user_id: int, granted_by: int, permission: str = "view") -> int:
        """共享 KB 给指定用户，并自动将 scope 设为 'shared'。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO kb_shares (kb_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (kb_id, user_id, permission, granted_by),
            )
            # 自动将 scope 设为 shared
            self._conn.execute(
                "UPDATE kb_metadata SET scope = 'shared' WHERE kb_id = ? AND scope != 'shared'",
                (kb_id,),
            )
            self._conn.commit()
            return cur.lastrowid

    def unshare_kb(self, kb_id: str, user_id: int) -> None:
        """取消 KB 共享，如果没有剩余共享用户则自动将 scope 设回 'private'。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM kb_shares WHERE kb_id = ? AND user_id = ?",
                (kb_id, user_id),
            )
            # 检查是否还有其他共享用户
            remaining = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM kb_shares WHERE kb_id = ?",
                (kb_id,),
            ).fetchone()
            if remaining["cnt"] == 0:
                self._conn.execute(
                    "UPDATE kb_metadata SET scope = 'private' WHERE kb_id = ? AND scope = 'shared'",
                    (kb_id,),
                )
            self._conn.commit()

    def list_kb_shared_users(self, kb_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.id, u.username, ks.permission, ks.granted_by, ks.created_at AS granted_at "
                "FROM kb_shares ks JOIN users u ON ks.user_id = u.id "
                "WHERE ks.kb_id = ? ORDER BY ks.created_at",
                (kb_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Document Versions (trusted knowledge loop)
    # ------------------------------------------------------------------

    def create_document_version(
        self,
        document_id: int,
        original_filename: str,
        file_path: str,
        content_hash: str,
        uploaded_by: int,
    ) -> int:
        """Reserve a staging version row and return its id.

        The row is ``staging`` until ``activate_document_version`` promotes
        it. ``version_no`` is derived per document as max+1.
        """
        with self._lock, self.transaction(immediate=True):
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no "
                "FROM document_versions WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            version_no = int(row["next_no"])
            cur = self._conn.execute(
                "INSERT INTO document_versions "
                "(document_id, version_no, original_filename, file_path, content_hash, uploaded_by, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'staging')",
                (document_id, version_no, original_filename, file_path, content_hash, uploaded_by),
            )
            return int(cur.lastrowid)

    def _activate_document_version_in_tx(
        self, document_id: int, version_id: int, kb_id: str, filename: str
    ) -> None:
        """Promote a staging version inside an already-open transaction.

        This row-mutation primitive never begins, commits, or rolls back a
        transaction; the caller owns exactly one.
        """
        now = time.time()
        self._conn.execute(
            "UPDATE document_versions SET status='historical', superseded_at=? "
            "WHERE document_id=? AND status='current'",
            (now, document_id),
        )
        activated = self._conn.execute(
            "UPDATE document_versions SET status='current', validity_status='valid' "
            "WHERE id=? AND document_id=? AND status='staging'",
            (version_id, document_id),
        )
        if activated.rowcount != 1:
            raise VersionConflict("version is no longer staging")
        self._conn.execute(
            "UPDATE document_permissions SET current_version_id=?, updated_at=? WHERE id=?",
            (version_id, now, document_id),
        )
        self._conn.execute(
            "UPDATE kb_documents SET document_id=?, document_version_id=? "
            "WHERE kb_id=? AND filename=?",
            (document_id, version_id, kb_id, filename),
        )

    def activate_document_version(
        self, document_id: int, version_id: int, kb_id: str, filename: str
    ) -> None:
        """Atomically promote a staging version to current."""
        with self._lock, self.transaction(immediate=True):
            self._activate_document_version_in_tx(document_id, version_id, kb_id, filename)

    def get_current_document_version(self, document_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at "
                "FROM document_versions WHERE document_id = ? AND status = 'current'",
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_document_versions(self, document_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at "
                "FROM document_versions WHERE document_id = ? ORDER BY version_no DESC",
                (document_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_version_validity(self, version_id: int, validity_status: str) -> None:
        if validity_status not in {"valid", "reported_outdated", "confirmed_outdated"}:
            raise ValueError(f"invalid validity status: {validity_status}")
        with self._lock:
            self._conn.execute(
                "UPDATE document_versions SET validity_status = ? WHERE id = ?",
                (validity_status, version_id),
            )
            self._conn.commit()

    def get_document_version_by_id(self, version_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at "
                "FROM document_versions WHERE id = ?",
                (version_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_version_file_path(self, version_id: int, file_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE document_versions SET file_path = ? WHERE id = ?",
                (file_path, version_id),
            )
            self._conn.commit()

    def delete_document_version(self, version_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM document_versions WHERE id = ?", (version_id,)
            )
            self._conn.commit()

    def set_version_status(self, version_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE document_versions SET status = ? WHERE id = ?",
                (status, version_id),
            )
            self._conn.commit()

    def list_document_permissions_all(self) -> list[dict[str, Any]]:
        """Return every logical document for startup migration and metrics."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_name, kb_id, owner_id, current_version_id "
                "FROM document_permissions ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Material lifecycle schema (incremental material plan)
    # ------------------------------------------------------------------

    def foreign_key_check(self) -> list[tuple]:
        with self._lock:
            rows = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        return [tuple(r) for r in rows]

    # ------------------------------------------------------------------
    # Material index jobs
    # ------------------------------------------------------------------

    def enqueue_latest_index_job(
        self, document_id: int, version_id: int, content_hash: str, not_before: float
    ) -> int:
        """Enqueue the latest index job for a document, cancelling prior active ones.

        Enqueue/cancel share one transaction so replacement debounces to the
        newest version without a window for stale jobs to win.
        """
        now = time.time()
        with self._lock, self.transaction(immediate=True):
            self._conn.execute(
                "UPDATE document_index_jobs SET status='cancelled', updated_at=? "
                "WHERE document_id=? AND status IN ('queued','retry','running')",
                (now, document_id),
            )
            cur = self._conn.execute(
                "INSERT INTO document_index_jobs "
                "(document_id, version_id, content_hash, status, available_at, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (document_id, version_id, content_hash, not_before, now, now),
            )
            return int(cur.lastrowid)

    def enqueue_material_job(self, document_id: int, version_id: int) -> int:
        """Enqueue a job immediately (test and maintenance helper)."""
        now = time.time()
        with self._lock, self.transaction(immediate=True):
            cur = self._conn.execute(
                "INSERT INTO document_index_jobs "
                "(document_id, version_id, content_hash, status, available_at, created_at, updated_at) "
                "VALUES (?, ?, '', 'queued', 0, ?, ?)",
                (document_id, version_id, now, now),
            )
            return int(cur.lastrowid)

    def get_index_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM document_index_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

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
        with self._lock, self.transaction(immediate=True):
            candidates = self._conn.execute(
                "SELECT id FROM document_index_jobs "
                "WHERE status IN ('queued','retry','running') AND available_at <= ? "
                "AND (lease_expires_at IS NULL OR lease_expires_at < ?) "
                "ORDER BY available_at LIMIT ?",
                (now, now, limit),
            ).fetchall()
            for row in candidates:
                # Reclaim queued/retry jobs, or running jobs whose lease expired.
                cur = self._conn.execute(
                    "UPDATE document_index_jobs "
                    "SET status='running', lease_owner=?, lease_expires_at=?, "
                    "attempt=attempt+1, updated_at=? "
                    "WHERE id=? AND available_at <= ? "
                    "AND (status IN ('queued','retry') "
                    "     OR (status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?))",
                    (worker_id, lease_expires, now, row["id"], now, now),
                )
                if cur.rowcount == 1:
                    job_row = self._conn.execute(
                        "SELECT * FROM document_index_jobs WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                    claimed.append(dict(job_row))
        return claimed

    def create_material_schema(self) -> None:
        """Apply material-lifecycle schema migrations idempotently.

        Adds logical metadata columns, rebuilds ``document_versions`` to widen
        its status CHECK for recycle/purge states, and creates the job, archive,
        organization, and FTS tables.
        """
        with self._lock:
            for column, ddl in (
                ("logical_path", "TEXT NOT NULL DEFAULT ''"),
                ("document_type", "TEXT NOT NULL DEFAULT ''"),
                ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("subject_user_id", "INTEGER"),
                ("metadata_version", "INTEGER NOT NULL DEFAULT 1"),
                ("deleted_at", "REAL"),
                ("deleted_by", "INTEGER"),
                ("restore_until", "REAL"),
                ("container_document_id", "INTEGER"),
                ("container_relative_path", "TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    self._conn.execute(f"SELECT {column} FROM document_permissions LIMIT 1")
                except sqlite3.OperationalError:
                    self._conn.execute(
                        f"ALTER TABLE document_permissions ADD COLUMN {column} {ddl}"
                    )
                    self._conn.commit()
            self._rebuild_document_versions_status()
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_index_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    version_id INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    job_type TEXT NOT NULL DEFAULT 'material_index',
                    status TEXT NOT NULL DEFAULT 'queued',
                    payload_hash TEXT NOT NULL DEFAULT '',
                    available_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE INDEX IF NOT EXISTS idx_document_index_jobs_active
                ON document_index_jobs(document_id, status);

                CREATE TABLE IF NOT EXISTS material_archive_manifests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    container_document_id INTEGER NOT NULL,
                    container_version_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_confirmation',
                    entry_count INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    reason_code TEXT NOT NULL DEFAULT '',
                    created_by INTEGER NOT NULL,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS material_archive_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manifest_id INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    normalized_path TEXT NOT NULL,
                    child_source_key TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'pending',
                    reason_code TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(manifest_id) REFERENCES material_archive_manifests(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS material_organization_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id INTEGER NOT NULL,
                    instruction_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS material_organization_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id INTEGER NOT NULL,
                    document_id INTEGER NOT NULL,
                    target_path TEXT NOT NULL,
                    add_tags TEXT NOT NULL DEFAULT '[]',
                    remove_tags TEXT NOT NULL DEFAULT '[]',
                    document_type TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reason_code TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(proposal_id) REFERENCES material_organization_proposals(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS material_organization_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id INTEGER,
                    actor_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'applied',
                    created_at REAL NOT NULL,
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    UNIQUE(proposal_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS material_organization_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL,
                    document_id INTEGER NOT NULL,
                    before_logical_path TEXT NOT NULL DEFAULT '',
                    after_logical_path TEXT NOT NULL DEFAULT '',
                    before_metadata_version INTEGER NOT NULL DEFAULT 0,
                    after_metadata_version INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'applied',
                    reason_code TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(batch_id) REFERENCES material_organization_batches(id) ON DELETE CASCADE
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS material_search_fts USING fts5(
                    document_id UNINDEXED,
                    version_id UNINDEXED,
                    filename,
                    logical_path,
                    document_type,
                    tags,
                    extracted_text
                );
                """
            )

    def _rebuild_document_versions_status(self) -> None:
        """Widen the ``document_versions`` status CHECK for material states."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='document_versions'"
        ).fetchone()
        if row is None or "recycle_pending" in (row["sql"] or ""):
            return
        indexes = [
            dict(r)
            for r in self._conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='document_versions' AND sql IS NOT NULL"
            ).fetchall()
        ]
        triggers = [
            dict(r)
            for r in self._conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='document_versions'"
            ).fetchall()
        ]
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "ALTER TABLE document_versions RENAME TO document_versions_old"
            )
            self._conn.execute(
                "CREATE TABLE document_versions ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " document_id INTEGER NOT NULL,"
                " version_no INTEGER NOT NULL,"
                " original_filename TEXT NOT NULL,"
                " file_path TEXT NOT NULL,"
                " content_hash TEXT NOT NULL,"
                " uploaded_by INTEGER NOT NULL,"
                " status TEXT NOT NULL CHECK(status IN "
                "('staging','indexing','current','historical','recycle_pending','failed','purged')),"
                " validity_status TEXT NOT NULL CHECK(validity_status IN "
                "('valid','reported_outdated','confirmed_outdated')) DEFAULT 'valid',"
                " created_at REAL NOT NULL DEFAULT (strftime('%s','now')),"
                " superseded_at REAL,"
                " UNIQUE(document_id, version_no),"
                " FOREIGN KEY(document_id) REFERENCES document_permissions(id) ON DELETE CASCADE,"
                " FOREIGN KEY(uploaded_by) REFERENCES users(id)"
                ")"
            )
            self._conn.execute(
                "INSERT INTO document_versions "
                "(id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at) "
                "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at "
                "FROM document_versions_old"
            )
            old_count = self._conn.execute(
                "SELECT COUNT(*) FROM document_versions_old"
            ).fetchone()[0]
            new_count = self._conn.execute(
                "SELECT COUNT(*) FROM document_versions"
            ).fetchone()[0]
            if old_count != new_count:
                raise RuntimeError(
                    f"document_versions rebuild row mismatch: {old_count} != {new_count}"
                )
            self._conn.execute("DROP TABLE document_versions_old")
            for index in indexes:
                self._conn.execute(index["sql"])
            for trigger in triggers:
                self._conn.execute(trigger["sql"])
            violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"foreign key violations: {violations}")
            self._conn.commit()
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()
