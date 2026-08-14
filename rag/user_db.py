"""SQLite-backed user database for conversations, messages, and feedback."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit as _urlsplit
from urllib.parse import urlunsplit as _urlunsplit

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

_QUERY_SECRET_PARAMS = (
    "password",
    "token",
    "access_token",
    "api_key",
    "apikey",
    "secret",
    "signature",
    "key",
    "credential",
)


def _mask_url_credentials(value: str) -> tuple[str, bool]:
    """脱敏任意 URL 中的凭据：userinfo（``user:pass@host``）与敏感 query 参数。

    返回 (脱敏后的 URL, 是否有敏感信息被掩掉)。非 URL 或无可脱敏内容时原样返回。
    """
    if not isinstance(value, str) or "://" not in value:
        return value, False
    redacted = False
    try:
        parts = _urlsplit(value)
        # 1) userinfo：user:pass@host → user:***@host；仅 user@host → ***@host
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, host = netloc.rsplit("@", 1)
            if ":" in userinfo:
                user = userinfo.split(":", 1)[0]
                netloc = f"{user}:***@{host}"
            else:
                netloc = f"***@{host}"
            redacted = True
        # 2) query 参数：?password=.. & ?token=.. 等敏感键值替换为 ***
        query = parts.query
        if query:
            pairs = []
            for pair in query.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if v and any(s in k.lower() for s in _QUERY_SECRET_PARAMS):
                        pairs.append(f"{k}=***")
                        redacted = True
                    else:
                        pairs.append(pair)
                else:
                    pairs.append(pair)
            query = "&".join(pairs)
        return (
            _urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment)),
            redacted,
        )
    except Exception:
        return value, True


def mask_data_source_config(config: dict) -> tuple[dict, bool]:
    """递归脱敏数据源 config，返回 (脱敏后的 config, has_secret)。

    - 任意含 ``://`` 的 URL 值（``url`` / ``connection_string`` 等键名不限）
      统一走 ``_mask_url_credentials``，掩掉 userinfo 口令与敏感 query 参数；
    - ``password``/``token``/``api_key`` 等敏感键值替换为 ``"***"``；
    - 嵌套 dict（如 API 的 ``headers``）同样处理。
    """
    masked: dict = {}
    has_secret = False
    for key, value in config.items():
        low = str(key).lower()
        if isinstance(value, dict):
            sub_masked, sub_has = mask_data_source_config(value)
            masked[key] = sub_masked
            has_secret = has_secret or sub_has
        elif isinstance(value, str) and "://" in value:
            masked_val, redacted = _mask_url_credentials(value)
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
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._create_tables()
        from rag.db_migrations import run_migrations
        from rag.knowledge_tasks import migrate_knowledge_task_types

        run_migrations(self._conn)
        migrate_knowledge_task_types(self._conn)

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
                    confidence_level TEXT NOT NULL DEFAULT 'unavailable',
                    confidence_reasons_json TEXT NOT NULL DEFAULT '[]',
                    answerability_kind TEXT NOT NULL DEFAULT 'answerable',
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
                    restored_from_version_id INTEGER REFERENCES document_versions(id),
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

                CREATE TABLE IF NOT EXISTS document_comparisons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL REFERENCES users(id),
                    question TEXT NOT NULL,
                    dimensions_json TEXT NOT NULL,
                    document_snapshot_json TEXT NOT NULL,
                    partial INTEGER NOT NULL,
                    failures_json TEXT NOT NULL,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS comparison_cells (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    comparison_id INTEGER NOT NULL REFERENCES document_comparisons(id) ON DELETE CASCADE,
                    dimension TEXT NOT NULL,
                    document_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('supported', 'insufficient_evidence')),
                    text TEXT NOT NULL,
                    UNIQUE(comparison_id, dimension, document_id)
                );

                CREATE TABLE IF NOT EXISTS comparison_citations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cell_id INTEGER NOT NULL REFERENCES comparison_cells(id) ON DELETE CASCADE,
                    document_id INTEGER NOT NULL,
                    document_version_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    section_title TEXT NOT NULL,
                    text_preview TEXT NOT NULL,
                    rank INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS structured_datasets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL REFERENCES document_permissions(id) ON DELETE CASCADE,
                    document_version_id INTEGER NOT NULL UNIQUE REFERENCES document_versions(id) ON DELETE CASCADE,
                    format TEXT NOT NULL CHECK(format IN ('xlsx', 'csv')),
                    status TEXT NOT NULL CHECK(status IN ('staging', 'ready', 'failed', 'historical')),
                    storage_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL DEFAULT '',
                    sheet_count INTEGER NOT NULL DEFAULT 0,
                    total_rows INTEGER NOT NULL DEFAULT 0,
                    total_columns INTEGER NOT NULL DEFAULT 0,
                    semantic_index_mode TEXT NOT NULL CHECK(semantic_index_mode IN ('schema_only', 'row_groups')),
                    extraction_version TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    ready_at REAL,
                    last_error_code TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_structured_datasets_document
                    ON structured_datasets(document_id, status);

                CREATE TABLE IF NOT EXISTS structured_sheets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dataset_id INTEGER NOT NULL REFERENCES structured_datasets(id) ON DELETE CASCADE,
                    sheet_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('visible', 'hidden', 'very_hidden')),
                    header_row INTEGER,
                    first_data_row INTEGER,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    column_count INTEGER NOT NULL DEFAULT 0,
                    merged_range_count INTEGER NOT NULL DEFAULT 0,
                    has_formulas INTEGER NOT NULL DEFAULT 0 CHECK(has_formulas IN (0, 1)),
                    schema_hash TEXT NOT NULL DEFAULT '',
                    UNIQUE(dataset_id, sheet_key),
                    UNIQUE(dataset_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS structured_columns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sheet_id INTEGER NOT NULL REFERENCES structured_sheets(id) ON DELETE CASCADE,
                    column_key TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    inferred_type TEXT NOT NULL CHECK(inferred_type IN ('text', 'integer', 'decimal', 'boolean', 'date', 'datetime')),
                    number_format TEXT NOT NULL DEFAULT '',
                    unit TEXT NOT NULL DEFAULT '',
                    nullable INTEGER NOT NULL DEFAULT 1 CHECK(nullable IN (0, 1)),
                    sensitivity_label TEXT NOT NULL DEFAULT 'none',
                    sample_values_json TEXT NOT NULL DEFAULT '[]',
                    inference_confidence REAL NOT NULL DEFAULT 0,
                    UNIQUE(sheet_id, column_key),
                    UNIQUE(sheet_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS structured_query_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    assistant_message_id INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL,
                    dataset_version_ids TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    used_columns_json TEXT NOT NULL,
                    filter_summary_json TEXT NOT NULL,
                    rows_scanned INTEGER NOT NULL DEFAULT 0,
                    rows_returned INTEGER NOT NULL DEFAULT 0,
                    result_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    duration_ms INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS vector_flag_repairs (
                    document_id INTEGER NOT NULL REFERENCES document_permissions(id) ON DELETE CASCADE,
                    version_id INTEGER NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    PRIMARY KEY(document_id, version_id)
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

                CREATE TABLE IF NOT EXISTS knowledge_task_refusal_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    active_key TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    question_key TEXT NOT NULL,
                    document_id INTEGER,
                    created_at REAL NOT NULL,
                    UNIQUE(active_key, user_id),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(document_id) REFERENCES document_permissions(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    active_key TEXT UNIQUE,
                    subject_key TEXT NOT NULL,
                    attribute_key TEXT NOT NULL,
                    unit_key TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    citation_ids_json TEXT NOT NULL,
                    document_ids_json TEXT NOT NULL,
                    document_version_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open', 'resolved', 'dismissed')),
                    resolution TEXT,
                    resolution_note TEXT NOT NULL DEFAULT '',
                    resolved_by INTEGER REFERENCES users(id),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    resolved_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_conflicts_status
                    ON knowledge_conflicts(status, updated_at);

                CREATE TABLE IF NOT EXISTS knowledge_conflict_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conflict_id INTEGER NOT NULL REFERENCES knowledge_conflicts(id) ON DELETE CASCADE,
                    actor_id INTEGER NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    resolution TEXT NOT NULL,
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

            # scope is the canonical visibility value; keep the legacy display
            # field synchronized for existing rows created before that rule.
            self._conn.execute("""
                UPDATE document_permissions
                SET is_public = CASE WHEN scope = 'public' THEN 1 ELSE 0 END
                WHERE is_public != CASE WHEN scope = 'public' THEN 1 ELSE 0 END
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

            # Answer reliability: explainable assessment persisted with answers.
            for column, ddl in (
                ("confidence_level", "TEXT NOT NULL DEFAULT 'unavailable'"),
                ("confidence_reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("answerability_kind", "TEXT NOT NULL DEFAULT 'answerable'"),
            ):
                try:
                    self._conn.execute(
                        f"SELECT {column} FROM chat_messages LIMIT 1"
                    )
                except sqlite3.OperationalError:
                    self._conn.execute(
                        f"ALTER TABLE chat_messages ADD COLUMN {column} {ddl}"
                    )
            self._conn.commit()

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
                ("purge_state", "TEXT NOT NULL DEFAULT ''"),
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

    def update_kb_overview(self, kb_id: str, overview: str) -> None:
        """Persist a KB overview through the shared, lock-protected connection."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO kb_metadata (kb_id, name, overview) VALUES (?, '', ?) "
                "ON CONFLICT(kb_id) DO UPDATE SET overview = ?",
                (kb_id, overview, overview),
            )
            self._conn.commit()

    def update_kb_document_toc(self, kb_id: str, filename: str, toc: str) -> bool:
        """Update a document TOC without opening a competing SQLite connection."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE kb_documents SET toc = ? WHERE kb_id = ? AND filename = ?",
                (toc, kb_id, filename),
            )
            self._conn.commit()
            return cur.rowcount > 0

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

    def ensure_development_user(self) -> dict[str, Any]:
        """Ensure the auth-disabled principal has a real FK-safe user row."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO users (id, username, salt, password, is_admin) "
                "VALUES (0, 'anonymous-development', '', '!disabled!', 1)"
            )
            self._conn.commit()
        user = self.get_user_by_id(0)
        if user is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("failed to create development principal")
        return user

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
        with self._lock:
            cur = self._conn.execute(
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
                    "SELECT m.id, m.conversation_id, m.role, m.content, "
                    "m.confidence_level, m.confidence_reasons_json, "
                    "m.answerability_kind, m.created_at "
                    "FROM chat_messages m "
                    "JOIN conversations c ON m.conversation_id = c.id "
                    "WHERE m.conversation_id = ? AND c.user_id = ? ORDER BY m.created_at",
                    (conversation_id, int(user_id)),
                ).fetchall()
            else:
                rows = self._conn.execute(
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
        with self._lock:
            row = self._conn.execute(
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
        if value not in {-1, 0, 1}:
            raise ValueError("feedback value 必须为 -1、0 或 1")
        with self._lock:
            owned = self._conn.execute(
                "SELECT 1 FROM chat_messages m "
                "JOIN conversations c ON m.conversation_id = c.id "
                "WHERE m.id = ? AND c.user_id = ?",
                (message_id, int(user_id)),
            ).fetchone()
            if owned is None:
                raise ValueError("消息不存在或无权操作")
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
        with self._lock, self.transaction(immediate=True):
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
        with self._lock, self.transaction(immediate=True):
            self._conn.execute(
                """
                INSERT INTO knowledge_task_refusal_reports (
                    active_key, user_id, question_key, document_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(active_key, user_id) DO UPDATE SET created_at = excluded.created_at
                """,
                (active_key, reporter_id, question_key, document_id, now),
            )
            report_count = int(
                self._conn.execute(
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
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_tasks_by_type(self, task_type: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_tasks WHERE task_type = ? ORDER BY id", (task_type,)
            ).fetchall()
        return [dict(row) for row in rows]

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

    def create_knowledge_conflict(
        self,
        *,
        active_key: str,
        subject_key: str,
        attribute_key: str,
        unit_key: str,
        scope_key: str,
        citation_ids: tuple[int, ...],
        document_ids: tuple[int, ...],
        document_version_ids: tuple[int, ...],
    ) -> dict[str, Any]:
        """Create one active conflict, or return its idempotent existing record."""
        now = time.time()
        citation_ids_json = json.dumps(sorted(set(citation_ids)))
        document_ids_json = json.dumps(sorted(set(document_ids)))
        document_version_ids_json = json.dumps(sorted(set(document_version_ids)))
        with self._lock, self.transaction(immediate=True):
            row = self._conn.execute(
                "SELECT id FROM knowledge_conflicts WHERE active_key = ? AND status = 'open'",
                (active_key,),
            ).fetchone()
            if row is None:
                cur = self._conn.execute(
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
        with self._lock:
            row = self._conn.execute(
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
        with self._lock, self.transaction(immediate=True):
            conflict = self._conn.execute(
                "SELECT status, document_ids_json FROM knowledge_conflicts WHERE id = ?",
                (conflict_id,),
            ).fetchone()
            if conflict is None:
                return None

            actor = self._conn.execute(
                "SELECT is_admin FROM users WHERE id = ?", (actor_id,)
            ).fetchone()
            if actor is None:
                raise PermissionError("not authorized to resolve conflict")

            document_ids = tuple(json.loads(conflict["document_ids_json"]))
            if not bool(actor["is_admin"]):
                if not document_ids:
                    raise PermissionError("not authorized to resolve conflict")
                placeholders = ",".join("?" for _ in document_ids)
                owners = self._conn.execute(
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
            self._conn.execute(
                "UPDATE knowledge_conflicts SET status = ?, active_key = NULL, resolution = ?, "
                "resolution_note = ?, resolved_by = ?, updated_at = ?, resolved_at = ? "
                "WHERE id = ?",
                (resolution, resolution, note, actor_id, now, now, conflict_id),
            )
            self._conn.execute(
                "INSERT INTO knowledge_conflict_events "
                "(conflict_id, actor_id, old_status, new_status, resolution, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conflict_id, actor_id, old_status, resolution, resolution, note, now),
            )
        return self.get_knowledge_conflict(conflict_id)

    def list_knowledge_conflict_events(self, conflict_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT actor_id, old_status, new_status, resolution, note, created_at "
                "FROM knowledge_conflict_events WHERE conflict_id = ? ORDER BY id ASC",
                (conflict_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
        confidence_level: str = "unavailable",
        confidence_reasons_json: str = "[]",
        answerability_kind: str = "answerable",
    ) -> int:
        """Insert one message row inside an already-open transaction."""
        cur = self._conn.execute(
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
        *,
        confidence_level: str = "unavailable",
        confidence_reasons: tuple[str, ...] | list[str] = (),
        answerability_kind: str = "answerable",
    ) -> int:
        """Persist one assistant answer and its citations atomically.

        Retrying with the same ``persistence_key`` returns the existing
        message only when content, citation hash, and count all match;
        otherwise a :class:`VersionConflict` is raised instead of silently
        storing different content.
        """
        citations_hash = self._citations_hash(citations)
        reasons_json = self._serialize_reliability(
            confidence_level, confidence_reasons, answerability_kind
        )
        with self._lock, self.transaction(immediate=True):
            row = self._conn.execute(
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
            message_id = self._insert_message_in_tx(
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
    # Persisted document comparisons
    # ------------------------------------------------------------------

    def persist_document_comparison(self, owner_id: int, request, execution):
        """Persist comparison output and its independent citations atomically."""
        from rag.reliability.comparison import ComparisonCell, ComparisonResult, ComparisonRow

        snapshot = sorted(
            {
                (citation.document_id, citation.document_version_id)
                for row in execution.rows
                for cell in row.values.values()
                for citation in cell.citations
            }
        )
        with self._lock, self.transaction(immediate=True):
            comparison_cur = self._conn.execute(
                "INSERT INTO document_comparisons "
                "(owner_id, question, dimensions_json, document_snapshot_json, partial, failures_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    owner_id,
                    request.question,
                    json.dumps(request.dimensions),
                    json.dumps(snapshot),
                    int(execution.partial),
                    json.dumps(execution.failures),
                ),
            )
            comparison_id = int(comparison_cur.lastrowid)
            rows = []
            for row in execution.rows:
                values = {}
                for document_id, cell in row.values.items():
                    if cell.status == "supported" and not cell.citations:
                        raise ValueError("supported comparison cell requires citation")
                    if cell.status == "insufficient_evidence" and (cell.text or cell.citations):
                        raise ValueError("insufficient evidence comparison cell must be empty")
                    cell_cur = self._conn.execute(
                        "INSERT INTO comparison_cells "
                        "(comparison_id, dimension, document_id, status, text) VALUES (?, ?, ?, ?, ?)",
                        (comparison_id, row.dimension, document_id, cell.status, cell.text),
                    )
                    cell_id = int(cell_cur.lastrowid)
                    citation_ids = []
                    for citation in cell.citations:
                        if citation.document_id != document_id:
                            raise ValueError("comparison citation document mismatch")
                        citation_cur = self._conn.execute(
                            "INSERT INTO comparison_citations "
                            "(cell_id, document_id, document_version_id, chunk_index, section_title, "
                            "text_preview, rank) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                cell_id,
                                citation.document_id,
                                citation.document_version_id,
                                citation.chunk_index,
                                citation.section_title,
                                citation.text_preview,
                                citation.rank,
                            ),
                        )
                        citation_ids.append(int(citation_cur.lastrowid))
                    values[int(document_id)] = ComparisonCell(
                        status=cell.status,
                        text=cell.text,
                        citation_ids=tuple(citation_ids),
                    )
                rows.append(ComparisonRow(dimension=row.dimension, values=values))
        return ComparisonResult(
            comparison_id=comparison_id,
            rows=tuple(rows),
            partial=execution.partial,
            failures=execution.failures,
        )

    def get_document_comparison(self, comparison_id: int, actor_id: int, is_admin: bool):
        """Return one stored comparison only to its owner or an administrator."""
        from rag.reliability.comparison import ComparisonCell, ComparisonResult, ComparisonRow

        with self._lock:
            comparison = self._conn.execute(
                "SELECT id, partial, failures_json FROM document_comparisons "
                "WHERE id = ? AND (? OR owner_id = ?)",
                (comparison_id, int(is_admin), actor_id),
            ).fetchone()
            if comparison is None:
                return None
            cell_rows = self._conn.execute(
                "SELECT id, dimension, document_id, status, text FROM comparison_cells "
                "WHERE comparison_id = ? ORDER BY id",
                (comparison_id,),
            ).fetchall()
            citations = self._conn.execute(
                "SELECT id, cell_id FROM comparison_citations WHERE cell_id IN "
                "(SELECT id FROM comparison_cells WHERE comparison_id = ?) ORDER BY id",
                (comparison_id,),
            ).fetchall()
        citation_ids_by_cell: dict[int, list[int]] = {}
        for citation in citations:
            citation_ids_by_cell.setdefault(int(citation["cell_id"]), []).append(int(citation["id"]))
        values_by_dimension: dict[str, dict[int, Any]] = {}
        for cell in cell_rows:
            values_by_dimension.setdefault(cell["dimension"], {})[int(cell["document_id"])] = ComparisonCell(
                status=cell["status"],
                text=cell["text"],
                citation_ids=tuple(citation_ids_by_cell.get(int(cell["id"]), [])),
            )
        return ComparisonResult(
            comparison_id=int(comparison["id"]),
            rows=tuple(
                ComparisonRow(dimension=dimension, values=values)
                for dimension, values in values_by_dimension.items()
            ),
            partial=bool(comparison["partial"]),
            failures=tuple(json.loads(comparison["failures_json"])),
        )

    def get_document_comparison_document_ids(
        self, comparison_id: int, actor_id: int, is_admin: bool
    ) -> tuple[int, ...] | None:
        """Return comparison source IDs only after the same ownership check."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT c.document_id FROM comparison_cells c "
                "JOIN document_comparisons d ON d.id = c.comparison_id "
                "WHERE c.comparison_id = ? AND (? OR d.owner_id = ?) ORDER BY c.document_id",
                (comparison_id, int(is_admin), actor_id),
            ).fetchall()
            exists = self._conn.execute(
                "SELECT 1 FROM document_comparisons WHERE id = ? AND (? OR owner_id = ?)",
                (comparison_id, int(is_admin), actor_id),
            ).fetchone()
        if exists is None:
            return None
        return tuple(int(row["document_id"]) for row in rows)

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
        is_public = scope == "public"
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
        "deleted_at, deleted_by, restore_until, container_document_id, container_relative_path, "
        "purge_state"
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
        d["is_public"] = d.get("scope") == "public"
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
        d["is_public"] = d.get("scope") == "public"
        d["protected"] = bool(d.get("protected", 0))
        d["downloadable"] = bool(d.get("downloadable", 1))
        return d

    def toggle_document_visibility(self, doc_id: int, is_public: bool | None = None) -> bool:
        """切换文档公开/私有状态，返回新的 is_public 值。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT scope FROM document_permissions WHERE id = ?", (doc_id,)
            ).fetchone()
            if not row:
                return False
            new_val = row["scope"] != "public" if is_public is None else bool(is_public)
            scope = "public" if new_val else "private"
            self._conn.execute(
                "UPDATE document_permissions SET is_public = ?, scope = ? WHERE id = ?",
                (int(new_val), scope, doc_id),
            )
            self._conn.commit()
            return new_val

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
                "OR (dp.scope = 'shared' AND dp.id IN "
                "(SELECT doc_id FROM document_shares WHERE user_id = ?)))",
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
            d["is_public"] = d.get("scope") == "public"
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
                f"SELECT id, doc_name, kb_id, owner_id, permission_level, is_public, protected, scope, created_at "
                f"FROM document_permissions WHERE id IN ({placeholders})",
                doc_ids,
            ).fetchall()
        result = {}
        for r in rows:
            d = dict(r)
            d["is_public"] = d.get("scope") == "public"
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
            cur = self._conn.execute(
                "DELETE FROM document_shares WHERE doc_id = ? AND user_id = ?",
                (doc_id, user_id),
            )
            if cur.rowcount:
                has_remaining_shares = self._conn.execute(
                    "SELECT 1 FROM document_shares WHERE doc_id = ? LIMIT 1", (doc_id,)
                ).fetchone()
                if not has_remaining_shares:
                    self._conn.execute(
                        "UPDATE document_permissions SET scope = 'private', is_public = 0 "
                        "WHERE id = ?",
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
    # Structured spreadsheet datasets
    # ------------------------------------------------------------------

    def create_structured_dataset(
        self, document_id: int, document_version_id: int, fmt: str, semantic_index_mode: str
    ) -> int:
        """Reserve one staging dataset bound to one immutable document version."""
        with self._lock, self.transaction(immediate=True):
            version = self._conn.execute(
                "SELECT document_id FROM document_versions WHERE id = ?", (document_version_id,)
            ).fetchone()
            if version is None or int(version["document_id"]) != document_id:
                raise ValueError("document version does not belong to document")
            storage_key = f"{document_id}/{document_version_id}/dataset.sqlite3"
            cur = self._conn.execute(
                "INSERT INTO structured_datasets "
                "(document_id, document_version_id, format, status, storage_key, semantic_index_mode) "
                "VALUES (?, ?, ?, 'staging', ?, ?)",
                (document_id, document_version_id, fmt, storage_key, semantic_index_mode),
            )
            return int(cur.lastrowid)

    def activate_structured_dataset(self, dataset_id: int) -> None:
        """Mark a fully validated staging dataset ready without changing document state."""
        with self._lock, self.transaction(immediate=True):
            updated = self._conn.execute(
                "UPDATE structured_datasets SET status = 'ready', ready_at = ? "
                "WHERE id = ? AND status = 'staging'",
                (time.time(), dataset_id),
            )
            if updated.rowcount != 1:
                raise VersionConflict("dataset is no longer staging")

    def _activate_structured_dataset_in_tx(self, document_id: int, dataset_id: int) -> None:
        """Promote a staging dataset while its caller owns the SQLite transaction."""
        updated = self._conn.execute(
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
        with self._lock, self.transaction(immediate=True):
            dataset = self._conn.execute(
                "SELECT document_version_id FROM structured_datasets "
                "WHERE id = ? AND document_id = ?",
                (dataset_id, document_id),
            ).fetchone()
            if dataset is None or int(dataset["document_version_id"]) != version_id:
                raise ValueError("dataset does not belong to document version")
            self._activate_document_version_in_tx(document_id, version_id, kb_id, filename)
            self._activate_structured_dataset_in_tx(document_id, dataset_id)

    def mark_structured_dataset_failed(self, dataset_id: int, error_code: str) -> None:
        """Record an extraction failure without altering any current-version pointer."""
        with self._lock:
            self._conn.execute(
                "UPDATE structured_datasets SET status = 'failed', last_error_code = ? "
                "WHERE id = ? AND status = 'staging'",
                (error_code, dataset_id),
            )
            self._conn.commit()

    def enqueue_vector_flag_repair(self, document_id: int, version_id: int) -> None:
        """Persist idempotent cleanup work after the authoritative SQLite switch succeeds."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO vector_flag_repairs (document_id, version_id) VALUES (?, ?)",
                (document_id, version_id),
            )
            self._conn.commit()

    def list_vector_flag_repairs(self) -> list[dict[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT document_id, version_id FROM vector_flag_repairs "
                "ORDER BY document_id, version_id"
            ).fetchall()
        return [{"document_id": int(row["document_id"]), "version_id": int(row["version_id"])} for row in rows]

    def get_structured_dataset_for_version(self, version_id: int) -> dict[str, Any] | None:
        """Return metadata for exactly one document version, never a current-version fallback."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, document_id, document_version_id, format, status, storage_key, "
                "content_hash, sheet_count, total_rows, total_columns, semantic_index_mode, "
                "extraction_version, created_at, ready_at, last_error_code "
                "FROM structured_datasets WHERE document_version_id = ?",
                (version_id,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Document Versions (trusted knowledge loop)
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_json_text(value: Any) -> str:
        """Return deterministic JSON for a value or an existing JSON string."""
        decoded = json.loads(value) if isinstance(value, str) else value
        return json.dumps(
            decoded,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

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
        with self._lock, self.transaction(immediate=True):
            current = self._conn.execute(
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
                source = self._conn.execute(
                    "SELECT document_id FROM document_versions WHERE id = ?",
                    (restored_from_version_id,),
                ).fetchone()
                if source is None or source["document_id"] != document_id:
                    raise VersionConflict("spreadsheet_restore_source_conflict")

            next_row = self._conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no "
                "FROM document_versions WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            version_cur = self._conn.execute(
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
            session_cur = self._conn.execute(
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
            self._conn.executemany(
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
                self._conn.execute(
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
        with self._lock:
            row = self._conn.execute(
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
        with self._lock, self.transaction(immediate=True):
            cur = self._conn.execute(
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
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM spreadsheet_edit_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_spreadsheet_edit_session_for_version(
        self, version_id: int
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
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
        with self._lock, self.transaction(immediate=True):
            row = self._conn.execute(
                "SELECT status FROM spreadsheet_edit_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None or status not in allowed[row["status"]]:
                raise VersionConflict("spreadsheet_edit_status_conflict")
            active_time = (
                activated_at if activated_at is not None else time.time()
            ) if status == "active" else None
            self._conn.execute(
                "UPDATE spreadsheet_edit_sessions "
                "SET status = ?, activated_at = ?, error_code = ? WHERE id = ?",
                (status, active_time, error_code, session_id),
            )

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
                "uploaded_by, status, validity_status, created_at, superseded_at, "
                "restored_from_version_id "
                "FROM document_versions WHERE document_id = ? AND status = 'current'",
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_document_versions(self, document_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at, "
                "restored_from_version_id "
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
                "uploaded_by, status, validity_status, created_at, superseded_at, "
                "restored_from_version_id "
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
        with self._lock, self.transaction(immediate=True):
            cur = self._conn.execute(
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
        with self._lock, self.transaction(immediate=True):
            cur = self._conn.execute(
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
                ("purge_state", "TEXT NOT NULL DEFAULT ''"),
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
            try:
                self._conn.execute("SELECT source_path FROM material_archive_manifests LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute(
                    "ALTER TABLE material_archive_manifests "
                    "ADD COLUMN source_path TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS material_archive_import_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manifest_id INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    container_document_id INTEGER NOT NULL,
                    container_version_id INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    UNIQUE(manifest_id, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_material_archive_entries_key
                ON material_archive_entries(child_source_key);

                CREATE INDEX IF NOT EXISTS idx_doc_permissions_container
                ON document_permissions(container_document_id);

                CREATE TABLE IF NOT EXISTS material_unit_fingerprints (
                    document_id INTEGER NOT NULL,
                    version_id INTEGER NOT NULL,
                    unit_key TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY (version_id, unit_key)
                );

                CREATE INDEX IF NOT EXISTS idx_unit_fp_doc
                ON material_unit_fingerprints(document_id, version_id);

                CREATE TABLE IF NOT EXISTS material_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_material_audit_doc
                ON material_audit_log(document_id);
                """
            )
            try:
                self._conn.execute("SELECT state FROM answer_citations LIMIT 1")
            except sqlite3.OperationalError:
                self._conn.execute(
                    "ALTER TABLE answer_citations "
                    "ADD COLUMN state TEXT NOT NULL DEFAULT 'available'"
                )
                self._conn.commit()

    # ------------------------------------------------------------------
    # Archive manifest persistence
    # ------------------------------------------------------------------

    def create_material_archive_manifest(
        self,
        container_document_id: int,
        container_version_id: int,
        source_path: str,
        created_by: int,
        expires_at: float,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO material_archive_manifests "
                "(container_document_id, container_version_id, source_path, status, created_by, expires_at) "
                "VALUES (?, ?, ?, 'pending_confirmation', ?, ?)",
                (container_document_id, container_version_id, source_path, created_by, expires_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_material_archive_manifest(self, manifest_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM material_archive_manifests WHERE id = ?", (manifest_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_material_archive_manifest_status(self, manifest_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE material_archive_manifests SET status = ? WHERE id = ?",
                (status, manifest_id),
            )
            self._conn.commit()

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
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO material_archive_entries "
                "(manifest_id, relative_path, normalized_path, child_source_key, size_bytes, content_hash, state, reason_code) "
                "VALUES (?, ?, ?, ?, ?, '', ?, ?)",
                (manifest_id, relative_path, normalized_path, child_source_key, size_bytes, state, reason_code),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_material_archive_entries(self, manifest_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM material_archive_entries WHERE manifest_id = ? ORDER BY id",
                (manifest_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_material_archive_entry_content(self, entry_id: int, content_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE material_archive_entries SET content_hash = ? WHERE id = ?",
                (content_hash, entry_id),
            )
            self._conn.commit()

    def get_container_child(
        self, container_document_id: int, relative_path: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
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
        with self._lock:
            self._conn.execute(
                "UPDATE document_permissions "
                "SET container_document_id = ?, container_relative_path = ? WHERE id = ?",
                (container_document_id, relative_path, document_id),
            )
            self._conn.commit()

    def list_container_children(self, container_document_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dp.id AS document_id, dp.doc_name, dp.container_relative_path, "
                "dv.id AS version_id, dv.version_no, dv.content_hash "
                "FROM document_permissions dp "
                "JOIN document_versions dv ON dv.id = dp.current_version_id AND dv.status = 'current' "
                "WHERE dp.container_document_id = ? "
                "ORDER BY dp.container_relative_path",
                (container_document_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Archive confirmation results (idempotency) and material FTS
    # ------------------------------------------------------------------

    def get_archive_import_result(
        self, manifest_id: int, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
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
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO material_archive_import_results "
                "(manifest_id, idempotency_key, container_document_id, container_version_id, result_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (manifest_id, idempotency_key, container_document_id, container_version_id, result_json),
            )
            self._conn.commit()
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
        with self._lock:
            self._conn.execute(
                "INSERT INTO material_search_fts "
                "(document_id, version_id, filename, logical_path, document_type, tags, extracted_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, version_id, filename, logical_path, document_type, tags, extracted_text),
            )
            self._conn.commit()

    def search_material_search_fts(self, text: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            try:
                rows = self._conn.execute(
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

    # ------------------------------------------------------------------
    # Organization proposals, batches, and rollback
    # ------------------------------------------------------------------

    def create_organization_proposal(
        self, actor_id: int, instruction_hash: str, expires_at: float
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO material_organization_proposals "
                "(actor_id, instruction_hash, status, expires_at, created_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (actor_id, instruction_hash, expires_at, time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_organization_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM material_organization_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_organization_proposal_status(self, proposal_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE material_organization_proposals SET status = ? WHERE id = ?",
                (status, proposal_id),
            )
            self._conn.commit()

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
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO material_organization_items "
                "(proposal_id, document_id, target_path, add_tags, remove_tags, document_type, reason, confidence, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (proposal_id, document_id, target_path, add_tags, remove_tags, document_type, reason, confidence),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_organization_items(self, proposal_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM material_organization_items WHERE proposal_id = ? ORDER BY id",
                (proposal_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_organization_item_status(
        self, item_id: int, status: str, reason_code: str = ""
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE material_organization_items SET status = ?, reason_code = ? WHERE id = ?",
                (status, reason_code, item_id),
            )
            self._conn.commit()

    def get_organization_batch(
        self, proposal_id: int, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM material_organization_batches "
                "WHERE proposal_id = ? AND idempotency_key = ?",
                (proposal_id, idempotency_key),
            ).fetchone()
        return dict(row) if row else None

    def _create_organization_batch_in_tx(
        self, proposal_id: int, actor_id: int, idempotency_key: str
    ) -> int:
        """Insert a batch row inside an already-open transaction."""
        cur = self._conn.execute(
            "INSERT INTO material_organization_batches "
            "(proposal_id, actor_id, status, created_at, idempotency_key) "
            "VALUES (?, ?, 'applied', ?, ?)",
            (proposal_id, actor_id, time.time(), idempotency_key),
        )
        return int(cur.lastrowid)

    def create_organization_batch(
        self, proposal_id: int, actor_id: int, idempotency_key: str
    ) -> int:
        with self._lock:
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
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO material_organization_batch_items "
                "(batch_id, document_id, before_logical_path, after_logical_path, "
                "before_metadata_version, after_metadata_version, status, reason_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id, document_id, before_logical_path, after_logical_path,
                    before_metadata_version, after_metadata_version, status, reason_code,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_organization_batch_items(self, batch_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM material_organization_batch_items WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_organization_batch_by_id(self, batch_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM material_organization_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Incremental worker support
    # ------------------------------------------------------------------

    def current_version_id(self, document_id: int) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT current_version_id FROM document_permissions WHERE id = ?",
                (document_id,),
            ).fetchone()
        return row["current_version_id"] if row else None

    def get_latest_version_id(self, document_id: int) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM document_versions "
                "WHERE document_id = ? ORDER BY version_no DESC LIMIT 1",
                (document_id,),
            ).fetchone()
        return row["id"] if row else None

    def store_unit_fingerprints(
        self, document_id: int, version_id: int, fingerprints: dict[str, str]
    ) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM material_unit_fingerprints WHERE version_id = ?",
                (version_id,),
            )
            self._conn.executemany(
                "INSERT INTO material_unit_fingerprints "
                "(document_id, version_id, unit_key, digest) VALUES (?, ?, ?, ?)",
                [(document_id, version_id, key, digest) for key, digest in fingerprints.items()],
            )
            self._conn.commit()

    def load_unit_fingerprints(self, document_id: int, version_id: int) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT unit_key, digest FROM material_unit_fingerprints WHERE version_id = ?",
                (version_id,),
            ).fetchall()
        return {r["unit_key"]: r["digest"] for r in rows}

    # ------------------------------------------------------------------
    # Recycle support
    # ------------------------------------------------------------------

    def is_document_deleted(self, document_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT deleted_at FROM document_permissions WHERE id = ?",
                (document_id,),
            ).fetchone()
        return bool(row and row["deleted_at"] is not None)

    def list_recycled_documents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_name, kb_id, owner_id, deleted_at, deleted_by, restore_until "
                "FROM document_permissions WHERE deleted_at IS NOT NULL "
                "ORDER BY deleted_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_citation_state(self, document_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM answer_citations WHERE document_id = ? "
                "ORDER BY CASE state WHEN 'purged_by_retention_policy' THEN 0 ELSE 1 END "
                "LIMIT 1",
                (document_id,),
            ).fetchone()
        return row["state"] if row else "available"

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
        # Keep child-table foreign-key declarations pointing at the final
        # ``document_versions`` name while this legacy table is renamed.
        self._conn.execute("PRAGMA legacy_alter_table=ON")
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
                " restored_from_version_id INTEGER REFERENCES document_versions(id),"
                " UNIQUE(document_id, version_no),"
                " FOREIGN KEY(document_id) REFERENCES document_permissions(id) ON DELETE CASCADE,"
                " FOREIGN KEY(uploaded_by) REFERENCES users(id)"
                ")"
            )
            self._conn.execute(
                "INSERT INTO document_versions "
                "(id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at, restored_from_version_id) "
                "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at, restored_from_version_id "
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
            self._conn.execute("PRAGMA legacy_alter_table=OFF")
            self._conn.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()
