"""Small, ordered SQLite schema migration runner."""

import sqlite3
from collections.abc import Iterable

Migration = tuple[int, str]


USER_DB_MIGRATIONS: tuple[Migration, ...] = (
    (1, "-- baseline schema is created by UserDB._create_tables"),
    (
        2,
        """
        ALTER TABLE data_sources ADD COLUMN owner_id TEXT;
        UPDATE data_sources SET owner_id = 'admin' WHERE owner_id IS NULL OR owner_id = '';
        """,
    ),
    (
        3,
        """
        ALTER TABLE document_versions
        ADD COLUMN restored_from_version_id INTEGER REFERENCES document_versions(id);
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS spreadsheet_edit_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES document_permissions(id) ON DELETE CASCADE,
            base_version_id INTEGER NOT NULL REFERENCES document_versions(id),
            new_version_id INTEGER REFERENCES document_versions(id),
            actor_user_id INTEGER NOT NULL REFERENCES users(id),
            operation TEXT NOT NULL CHECK(operation IN ('edit', 'rollback')),
            status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'failed', 'cancelled', 'rejected')),
            change_count INTEGER NOT NULL DEFAULT 0 CHECK(change_count >= 0),
            restored_from_version_id INTEGER REFERENCES document_versions(id),
            document_name_snapshot TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            ip_address TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            risk_flags_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
            activated_at REAL,
            error_code TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS spreadsheet_cell_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES spreadsheet_edit_sessions(id) ON DELETE CASCADE,
            sheet_key TEXT NOT NULL,
            sheet_name_snapshot TEXT NOT NULL,
            cell_address TEXT NOT NULL,
            old_value_type TEXT NOT NULL CHECK(old_value_type IN ('text', 'number', 'date', 'boolean', 'blank')),
            old_value_json TEXT NOT NULL,
            new_value_type TEXT NOT NULL CHECK(new_value_type IN ('text', 'number', 'date', 'boolean', 'blank')),
            new_value_json TEXT NOT NULL,
            sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0, 1)),
            UNIQUE(session_id, sheet_key, cell_address)
        );

        CREATE INDEX IF NOT EXISTS idx_spreadsheet_edit_sessions_created
        ON spreadsheet_edit_sessions(created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_spreadsheet_edit_sessions_actor
        ON spreadsheet_edit_sessions(actor_user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_spreadsheet_edit_sessions_document
        ON spreadsheet_edit_sessions(document_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_spreadsheet_edit_sessions_status
        ON spreadsheet_edit_sessions(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_spreadsheet_edit_sessions_risk
        ON spreadsheet_edit_sessions(risk_flags_json, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_spreadsheet_cell_changes_session
        ON spreadsheet_cell_changes(session_id, sheet_key, cell_address);
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS spreadsheet_structure_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL UNIQUE REFERENCES spreadsheet_edit_sessions(id) ON DELETE CASCADE,
            action TEXT NOT NULL CHECK(action IN ('insert_rows', 'delete_rows', 'insert_columns', 'delete_columns', 'add_sheet')),
            sheet_key TEXT,
            sheet_name_snapshot TEXT,
            target_index INTEGER CHECK(target_index IS NULL OR target_index >= 1),
            count INTEGER NOT NULL DEFAULT 1 CHECK(count BETWEEN 1 AND 100),
            new_sheet_name TEXT,
            affected_nonempty_cells INTEGER NOT NULL DEFAULT 0 CHECK(affected_nonempty_cells >= 0),
            confirm_nonempty INTEGER NOT NULL DEFAULT 0 CHECK(confirm_nonempty IN (0, 1))
        );

        CREATE INDEX IF NOT EXISTS idx_spreadsheet_structure_changes_session
        ON spreadsheet_structure_changes(session_id);
        """,
    ),
)


def run_migrations(conn: sqlite3.Connection, migrations: Iterable[Migration] = USER_DB_MIGRATIONS) -> int:
    """Apply pending migrations in version order and return the resulting version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in sorted(migrations):
        if version <= current:
            continue
        try:
            conn.executescript(script)
        except sqlite3.OperationalError as exc:
            # New installations already contain the column in the baseline DDL.
            if "duplicate column name" not in str(exc).lower():
                raise
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        current = version
    return current
