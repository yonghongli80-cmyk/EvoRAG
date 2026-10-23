"""SQLite schema definitions and migrations for UserDB."""

from __future__ import annotations

import sqlite3


def create_tables(conn: sqlite3.Connection) -> None:
    """Create all initial tables, indexes, and schema additions."""
    conn.executescript(
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
        conn.execute("SELECT permission_level FROM users LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE users ADD COLUMN permission_level INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()

    # Add is_admin column to users if it doesn't exist (idempotent)
    try:
        conn.execute("SELECT is_admin FROM users LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"
        )
        conn.commit()

    # Add is_public column to document_permissions if it doesn't exist (idempotent)
    try:
        conn.execute("SELECT is_public FROM document_permissions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE document_permissions ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT 0"
        )
        conn.commit()

    # Add protected column to document_permissions if it doesn't exist (idempotent)
    try:
        conn.execute("SELECT protected FROM document_permissions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE document_permissions ADD COLUMN protected BOOLEAN NOT NULL DEFAULT 0"
        )
        conn.commit()

    # Add mode column to conversations if it doesn't exist (idempotent)
    try:
        conn.execute("SELECT mode FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'file'"
        )
        conn.commit()
    # Backfill NULL mode values to 'file' (for conversations created before mode column existed)
    conn.execute(
        "UPDATE conversations SET mode = 'file' WHERE mode IS NULL"
    )
    # Add summary column to analysis_cards if it doesn't exist (idempotent)
    try:
        conn.execute("SELECT summary FROM analysis_cards LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE analysis_cards ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()

    # Phase 1 migration: add owner_id and scope to kb_metadata if missing
    try:
        conn.execute("SELECT owner_id FROM kb_metadata LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE kb_metadata ADD COLUMN owner_id INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE kb_metadata ADD COLUMN scope TEXT DEFAULT 'private'")
        # Migrate old data
        conn.execute("UPDATE kb_metadata SET owner_id = 0, scope = 'public' WHERE user_id IS NULL")
        conn.execute("UPDATE kb_metadata SET owner_id = user_id, scope = 'private' WHERE user_id IS NOT NULL")
        conn.commit()

    # Phase 1 migration: add scope to document_permissions if missing
    try:
        conn.execute("SELECT scope FROM document_permissions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE document_permissions ADD COLUMN scope TEXT DEFAULT 'private'")
        # 用 CASE 一次性迁移，优先级：protected > is_public > 默认 private
        conn.execute("""
            UPDATE document_permissions SET scope = CASE
                WHEN protected = 1 THEN 'public'
                WHEN is_public = 1 THEN 'public'
                ELSE 'private'
            END
        """)
        conn.commit()

    # scope is the canonical visibility value; keep the legacy display
    # field synchronized for existing rows created before that rule.
    conn.execute("""
        UPDATE document_permissions
        SET is_public = CASE WHEN scope = 'public' THEN 1 ELSE 0 END
        WHERE is_public != CASE WHEN scope = 'public' THEN 1 ELSE 0 END
    """)
    conn.commit()

    # Phase 2 migration: add permission to document_shares if missing
    try:
        conn.execute("SELECT permission FROM document_shares LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE document_shares ADD COLUMN permission TEXT DEFAULT 'view'")
        conn.commit()

    # Phase 3 migration: add downloadable to document_permissions if missing
    try:
        conn.execute("SELECT downloadable FROM document_permissions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE document_permissions ADD COLUMN downloadable INTEGER DEFAULT 1")
        conn.commit()

    # Phase 1a migration: add password_changed_at to users if missing
    try:
        conn.execute("SELECT password_changed_at FROM users LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE users ADD COLUMN password_changed_at REAL DEFAULT NULL")
        conn.commit()

    # Data source ownership migration: add owner_id to data_sources if missing (idempotent)
    try:
        conn.execute("SELECT owner_id FROM data_sources LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE data_sources ADD COLUMN owner_id TEXT")
        conn.commit()
    # 旧数据（无 owner_id）默认归 admin 所有（仅管理员可见/可删）
    conn.execute(
        "UPDATE data_sources SET owner_id = 'admin' WHERE owner_id IS NULL OR owner_id = ''"
    )
    conn.commit()
    # Persist only a bounded diagnostic for retryable KB document work.
    try:
        conn.execute("SELECT last_error FROM kb_documents LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE kb_documents ADD COLUMN last_error TEXT DEFAULT ''")
        conn.commit()

    # Trusted knowledge loop migrations: version pointers on documents.
    try:
        conn.execute("SELECT current_version_id FROM document_permissions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE document_permissions ADD COLUMN current_version_id INTEGER")
        conn.commit()
    try:
        conn.execute("SELECT source_type FROM document_permissions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE document_permissions ADD COLUMN source_type TEXT NOT NULL DEFAULT 'upload'")
        conn.commit()
    try:
        conn.execute("SELECT source_key FROM document_permissions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE document_permissions ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
        conn.commit()
    try:
        conn.execute("SELECT updated_at FROM document_permissions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE document_permissions ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
        conn.commit()

    # Trusted knowledge loop migrations: citation persistence columns.
    try:
        conn.execute("SELECT persistence_key FROM chat_messages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN persistence_key TEXT")
        conn.execute("ALTER TABLE chat_messages ADD COLUMN citations_hash TEXT")
        conn.execute("ALTER TABLE chat_messages ADD COLUMN citation_count INTEGER DEFAULT 0")
        conn.commit()
    conn.execute(
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
            conn.execute(
                f"SELECT {column} FROM chat_messages LIMIT 1"
            )
        except sqlite3.OperationalError:
            conn.execute(
                f"ALTER TABLE chat_messages ADD COLUMN {column} {ddl}"
            )
    conn.commit()

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
            conn.execute(f"SELECT {column} FROM feedback LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                f"ALTER TABLE feedback ADD COLUMN {column} {ddl}"
            )
            conn.commit()

    # Trusted knowledge loop migrations: version pointers on kb_documents.
    try:
        conn.execute("SELECT document_id FROM kb_documents LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE kb_documents ADD COLUMN document_id INTEGER")
        conn.commit()
    try:
        conn.execute("SELECT document_version_id FROM kb_documents LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE kb_documents ADD COLUMN document_version_id INTEGER")
        conn.commit()

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
            conn.execute(f"SELECT {column} FROM document_permissions LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                f"ALTER TABLE document_permissions ADD COLUMN {column} {ddl}"
            )
            conn.commit()


def rebuild_document_versions_status(conn: sqlite3.Connection) -> None:
    """Widen the ``document_versions`` status CHECK for material states."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='document_versions'"
    ).fetchone()
    if row is None or "recycle_pending" in (row["sql"] or ""):
        return
    indexes = [
        dict(r)
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='document_versions' AND sql IS NOT NULL"
        ).fetchall()
    ]
    triggers = [
        dict(r)
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='document_versions'"
        ).fetchall()
    ]
    conn.execute("PRAGMA foreign_keys=OFF")
    # Keep child-table foreign-key declarations pointing at the final
    # ``document_versions`` name while this legacy table is renamed.
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "ALTER TABLE document_versions RENAME TO document_versions_old"
        )
        conn.execute(
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
        conn.execute(
            "INSERT INTO document_versions "
            "(id, document_id, version_no, original_filename, file_path, content_hash, "
            "uploaded_by, status, validity_status, created_at, superseded_at, restored_from_version_id) "
            "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
            "uploaded_by, status, validity_status, created_at, superseded_at, restored_from_version_id "
            "FROM document_versions_old"
        )
        old_count = conn.execute(
            "SELECT COUNT(*) FROM document_versions_old"
        ).fetchone()[0]
        new_count = conn.execute(
            "SELECT COUNT(*) FROM document_versions"
        ).fetchone()[0]
        if old_count != new_count:
            raise RuntimeError(
                f"document_versions rebuild row mismatch: {old_count} != {new_count}"
            )
        conn.execute("DROP TABLE document_versions_old")
        for index in indexes:
            conn.execute(index["sql"])
        for trigger in triggers:
            conn.execute(trigger["sql"])
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations: {violations}")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")



def create_material_schema(conn: sqlite3.Connection) -> None:
    """Apply material-lifecycle schema migrations idempotently.

    Adds logical metadata columns, rebuilds ``document_versions`` to widen
    its status CHECK for recycle/purge states, and creates the job, archive,
    organization, and FTS tables.
    """
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
            conn.execute(f"SELECT {column} FROM document_permissions LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                f"ALTER TABLE document_permissions ADD COLUMN {column} {ddl}"
            )
            conn.commit()
    rebuild_document_versions_status(conn)
    conn.executescript(
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
            before_tags_json TEXT,
            after_tags_json TEXT,
            before_document_type TEXT,
            after_document_type TEXT,
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
        conn.execute("SELECT source_path FROM material_archive_manifests LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE material_archive_manifests "
            "ADD COLUMN source_path TEXT NOT NULL DEFAULT ''"
        )
        conn.commit()
    conn.executescript(
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
        conn.execute("SELECT state FROM answer_citations LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE answer_citations "
            "ADD COLUMN state TEXT NOT NULL DEFAULT 'available'"
        )
        conn.commit()

