-- 003_spreadsheets.sql: Structured spreadsheet datasets, sheets, columns, edit sessions, and audits

CREATE TABLE IF NOT EXISTS structured_datasets (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id          INTEGER NOT NULL REFERENCES document_permissions(id) ON DELETE CASCADE,
    document_version_id  INTEGER NOT NULL UNIQUE REFERENCES document_versions(id) ON DELETE CASCADE,
    format               TEXT NOT NULL CHECK(format IN ('xlsx', 'csv')),
    status               TEXT NOT NULL CHECK(status IN ('staging', 'ready', 'failed', 'historical')),
    storage_key          TEXT NOT NULL,
    content_hash         TEXT NOT NULL DEFAULT '',
    sheet_count          INTEGER NOT NULL DEFAULT 0,
    total_rows           INTEGER NOT NULL DEFAULT 0,
    total_columns        INTEGER NOT NULL DEFAULT 0,
    semantic_index_mode  TEXT NOT NULL CHECK(semantic_index_mode IN ('schema_only', 'row_groups')),
    extraction_version   TEXT NOT NULL DEFAULT '',
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now')),
    ready_at             REAL,
    last_error_code      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_structured_datasets_document
    ON structured_datasets(document_id, status);

CREATE TABLE IF NOT EXISTS structured_sheets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id         INTEGER NOT NULL REFERENCES structured_datasets(id) ON DELETE CASCADE,
    sheet_key          TEXT NOT NULL,
    name               TEXT NOT NULL,
    ordinal            INTEGER NOT NULL,
    visibility         TEXT NOT NULL CHECK(visibility IN ('visible', 'hidden', 'very_hidden')),
    header_row         INTEGER,
    first_data_row     INTEGER,
    row_count          INTEGER NOT NULL DEFAULT 0,
    column_count       INTEGER NOT NULL DEFAULT 0,
    merged_range_count INTEGER NOT NULL DEFAULT 0,
    has_formulas       INTEGER NOT NULL DEFAULT 0 CHECK(has_formulas IN (0, 1)),
    schema_hash        TEXT NOT NULL DEFAULT '',
    UNIQUE(dataset_id, sheet_key),
    UNIQUE(dataset_id, ordinal)
);

CREATE TABLE IF NOT EXISTS structured_columns (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id             INTEGER NOT NULL REFERENCES structured_sheets(id) ON DELETE CASCADE,
    column_key           TEXT NOT NULL,
    ordinal              INTEGER NOT NULL,
    display_name         TEXT NOT NULL,
    normalized_name      TEXT NOT NULL,
    inferred_type        TEXT NOT NULL CHECK(inferred_type IN ('text', 'integer', 'decimal', 'boolean', 'date', 'datetime')),
    number_format        TEXT NOT NULL DEFAULT '',
    unit                 TEXT NOT NULL DEFAULT '',
    nullable             INTEGER NOT NULL DEFAULT 1 CHECK(nullable IN (0, 1)),
    sensitivity_label    TEXT NOT NULL DEFAULT 'none',
    sample_values_json   TEXT NOT NULL DEFAULT '[]',
    inference_confidence REAL NOT NULL DEFAULT 0,
    UNIQUE(sheet_id, column_key),
    UNIQUE(sheet_id, ordinal)
);

CREATE TABLE IF NOT EXISTS structured_query_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER REFERENCES users(id) ON DELETE SET NULL,
    assistant_message_id INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL,
    dataset_version_ids  TEXT NOT NULL,
    plan_json            TEXT NOT NULL,
    used_columns_json    TEXT NOT NULL,
    filter_summary_json  TEXT NOT NULL,
    rows_scanned         INTEGER NOT NULL DEFAULT 0,
    rows_returned        INTEGER NOT NULL DEFAULT 0,
    result_hash          TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL,
    error_code           TEXT NOT NULL DEFAULT '',
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now')),
    duration_ms          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS spreadsheet_edit_sessions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id              INTEGER NOT NULL REFERENCES document_permissions(id) ON DELETE CASCADE,
    base_version_id          INTEGER NOT NULL REFERENCES document_versions(id),
    new_version_id           INTEGER REFERENCES document_versions(id),
    actor_user_id            INTEGER NOT NULL REFERENCES users(id),
    operation                TEXT NOT NULL CHECK(operation IN ('edit', 'rollback')),
    status                   TEXT NOT NULL CHECK(status IN ('pending', 'active', 'failed', 'cancelled', 'rejected')),
    change_count             INTEGER NOT NULL DEFAULT 0 CHECK(change_count >= 0),
    restored_from_version_id INTEGER REFERENCES document_versions(id),
    document_name_snapshot   TEXT NOT NULL,
    reason                   TEXT NOT NULL DEFAULT '',
    ip_address               TEXT NOT NULL DEFAULT '',
    user_agent               TEXT NOT NULL DEFAULT '',
    risk_flags_json          TEXT NOT NULL DEFAULT '[]',
    created_at               REAL NOT NULL DEFAULT (strftime('%s','now')),
    activated_at             REAL,
    error_code               TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS spreadsheet_cell_changes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         INTEGER NOT NULL REFERENCES spreadsheet_edit_sessions(id) ON DELETE CASCADE,
    sheet_key          TEXT NOT NULL,
    sheet_name_snapshot TEXT NOT NULL,
    cell_address       TEXT NOT NULL,
    old_value_type     TEXT NOT NULL CHECK(old_value_type IN ('text', 'number', 'date', 'boolean', 'blank')),
    old_value_json     TEXT NOT NULL,
    new_value_type     TEXT NOT NULL CHECK(new_value_type IN ('text', 'number', 'date', 'boolean', 'blank')),
    new_value_json     TEXT NOT NULL,
    sensitive          INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0, 1)),
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

CREATE TABLE IF NOT EXISTS spreadsheet_structure_changes (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id             INTEGER NOT NULL UNIQUE REFERENCES spreadsheet_edit_sessions(id) ON DELETE CASCADE,
    action                 TEXT NOT NULL CHECK(action IN ('insert_rows', 'delete_rows', 'insert_columns', 'delete_columns', 'add_sheet')),
    sheet_key              TEXT,
    sheet_name_snapshot    TEXT,
    target_index           INTEGER CHECK(target_index IS NULL OR target_index >= 1),
    count                  INTEGER NOT NULL DEFAULT 1 CHECK(count BETWEEN 1 AND 100),
    new_sheet_name         TEXT,
    affected_nonempty_cells INTEGER NOT NULL DEFAULT 0 CHECK(affected_nonempty_cells >= 0),
    confirm_nonempty       INTEGER NOT NULL DEFAULT 0 CHECK(confirm_nonempty IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_spreadsheet_structure_changes_session
    ON spreadsheet_structure_changes(session_id);
