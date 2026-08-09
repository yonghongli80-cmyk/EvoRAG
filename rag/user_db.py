"""SQLite-backed user database for conversations, messages, and feedback."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from typing import Any
from urllib.parse import urlsplit as _urlsplit
from urllib.parse import urlunsplit as _urlunsplit


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


# ------------------------------------------------------------------
# Data source config masking
# ------------------------------------------------------------------

_SENSITIVE_CONFIG_HINTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "authorization",
    "credential",
    "signature",
)


def _mask_connection_string(value: str) -> tuple[str, bool]:
    """脱敏连接串中的口令。返回 (脱敏后的连接串, 是否有敏感信息被掩掉)。"""
    if "://" not in value or "@" not in value:
        return value, False
    try:
        parts = _urlsplit(value)
        userinfo, host = parts.netloc.rsplit("@", 1)
        if ":" in userinfo:
            user = userinfo.split(":", 1)[0]
            masked_netloc = f"{user}:***@{host}"
        else:
            # 只有用户名没有口令 → 也一并掩掉，避免泄露用户名
            masked_netloc = f"***@{host}"
        return (
            _urlunsplit((parts.scheme, masked_netloc, parts.path, parts.query, parts.fragment)),
            True,
        )
    except Exception:
        return value, True


def mask_data_source_config(config: dict) -> tuple[dict, bool]:
    """递归脱敏数据源 config，返回 (脱敏后的 config, has_secret)。

    ``password``、``connection_string``、``token``、``api_key`` 等敏感字段
    替换为 ``"***"``；嵌套 dict（如 API 的 headers）同样处理。
    """
    masked: dict = {}
    has_secret = False
    for key, value in config.items():
        low = str(key).lower()
        if isinstance(value, dict):
            sub_masked, sub_has = mask_data_source_config(value)
            masked[key] = sub_masked
            has_secret = has_secret or sub_has
        elif low == "connection_string" and isinstance(value, str):
            masked_val, redacted = _mask_connection_string(value)
            masked[key] = masked_val
            has_secret = has_secret or redacted
        elif any(hint in low for hint in _SENSITIVE_CONFIG_HINTS) and value not in (None, ""):
            masked[key] = "***"
            has_secret = True
        else:
            masked[key] = value
    return masked, has_secret


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
                    added_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(kb_id, filename)
                );

                CREATE TABLE IF NOT EXISTS data_sources (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT    NOT NULL,
                    type            TEXT    NOT NULL,
                    config          TEXT    NOT NULL DEFAULT '{}',
                    status          TEXT    NOT NULL DEFAULT 'inactive',
                    owner_id        TEXT,
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

            # Data source ownership migration: add owner_id to data_sources if missing (idempotent)
            try:
                self._conn.execute("SELECT owner_id FROM data_sources LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE data_sources ADD COLUMN owner_id TEXT")
                self._conn.commit()
            # 旧数据（无 owner_id）默认归 admin 所有（仅管理员可见/可删）
            self._conn.execute(
                "UPDATE data_sources SET owner_id = 'admin' WHERE owner_id IS NULL OR owner_id = ''"
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

    def get_messages(self, conversation_id: int, user_id: int | str) -> list[dict[str, Any]]:
        """Return all messages in a conversation (only if owned by user_id)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.id, m.conversation_id, m.role, m.content, m.created_at "
                "FROM chat_messages m "
                "JOIN conversations c ON m.conversation_id = c.id "
                "WHERE m.conversation_id = ? AND c.user_id = ? ORDER BY m.created_at",
                (conversation_id, int(user_id)),
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
    # Data Sources
    # ------------------------------------------------------------------

    def _is_admin(self, user_id: Any) -> bool:
        """判断 *user_id* 是否具备数据源 admin 权限（可见/删除全部数据源）。

        - ``None`` → True（旧调用方/系统上下文，向后兼容，不按 owner 过滤）
        - ``'admin'`` 或 ``'0'`` → True
        - 数字 id（int 或数字字符串）→ 查 users 表的 ``is_admin``
        - 其他（``'anonymous'``、API key 映射的用户 id 等）→ False
        """
        if user_id is None:
            return True
        uid = str(user_id)
        if uid in ("admin", "0"):
            return True
        if uid.isdigit():
            try:
                row = self._conn.execute(
                    "SELECT is_admin FROM users WHERE id = ?", (int(uid),)
                ).fetchone()
                if row is not None:
                    return bool(row["is_admin"])
            except (TypeError, ValueError, sqlite3.Error):
                return False
        return False

    def create_data_source(
        self,
        name: str,
        source_type: str,
        config: str,
        owner_id: Any = "admin",
    ) -> int:
        """Create a data source and return its id.

        *owner_id* 记录归属（TEXT）。旧调用方不传时默认 ``'admin'``（系统级）。
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO data_sources (name, type, config, owner_id) VALUES (?, ?, ?, ?)",
                (name, source_type, config, str(owner_id)),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_data_sources(self, user_id: Any = None) -> list[dict[str, Any]]:
        """Return data sources visible to *user_id*, newest first.

        admin（或 ``user_id=None``，旧调用方）可见全部；普通用户仅见自己的。
        ``config`` 已脱敏（敏感字段替换为 ``***``），并附带 ``has_secret`` 标记。
        """
        with self._lock:
            if self._is_admin(user_id):
                rows = self._conn.execute(
                    "SELECT id, name, type, config, status, last_synced_at, created_at, owner_id "
                    "FROM data_sources ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, name, type, config, status, last_synced_at, created_at, owner_id "
                    "FROM data_sources WHERE owner_id = ? ORDER BY created_at DESC",
                    (str(user_id),),
                ).fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            raw = item.pop("config")
            try:
                cfg = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                cfg = {}
            masked, has_secret = mask_data_source_config(cfg)
            item["config"] = masked
            item["has_secret"] = has_secret
            result.append(item)
        return result

    def get_data_source(self, source_id: int, user_id: Any = None) -> dict[str, Any] | None:
        """Return a single data source by id, or None.

        非 admin 仅能读取自己的数据源。返回**完整**（未脱敏）config，供 sync 使用。
        """
        with self._lock:
            if self._is_admin(user_id):
                row = self._conn.execute(
                    "SELECT id, name, type, config, status, last_synced_at, created_at, owner_id "
                    "FROM data_sources WHERE id = ?",
                    (source_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT id, name, type, config, status, last_synced_at, created_at, owner_id "
                    "FROM data_sources WHERE id = ? AND owner_id = ?",
                    (source_id, str(user_id)),
                ).fetchone()
        return dict(row) if row else None

    def delete_data_source(self, source_id: int, user_id: Any = None) -> bool:
        """Delete a data source.  Returns True if deleted.

        非 admin 仅能删除自己的数据源；非属主删除返回 ``False``。
        """
        with self._lock:
            if self._is_admin(user_id):
                cur = self._conn.execute(
                    "DELETE FROM data_sources WHERE id = ?",
                    (source_id,),
                )
            else:
                cur = self._conn.execute(
                    "DELETE FROM data_sources WHERE id = ? AND owner_id = ?",
                    (source_id, str(user_id)),
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

    def get_document_permission(self, doc_name: str, kb_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, doc_name, kb_id, owner_id, permission_level, is_public, protected, scope, downloadable, created_at "
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
                "SELECT id, doc_name, kb_id, owner_id, permission_level, is_public, protected, scope, downloadable, created_at "
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
            self._conn.commit()
            return cur.lastrowid

    def unshare_document(self, doc_id: int, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM document_shares WHERE doc_id = ? AND user_id = ?",
                (doc_id, user_id),
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
                "SELECT u.id, u.username, ds.granted_by, ds.created_at AS granted_at "
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
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()
