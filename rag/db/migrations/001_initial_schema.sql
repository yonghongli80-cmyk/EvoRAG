-- 001_initial_schema.sql: Core baseline schema for RAGv3

-- Users & authentication
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT    NOT NULL UNIQUE,
    salt                TEXT    NOT NULL,
    password            TEXT    NOT NULL,
    created_at          REAL    NOT NULL DEFAULT (strftime('%s','now')),
    permission_level    INTEGER NOT NULL DEFAULT 1,
    is_admin            BOOLEAN NOT NULL DEFAULT 0,
    password_changed_at REAL    DEFAULT NULL
);

-- Conversations
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT    NOT NULL DEFAULT '',
    mode        TEXT    NOT NULL DEFAULT 'file',
    created_at  REAL    NOT NULL DEFAULT (strftime('%s','now'))
);

-- Chat messages
CREATE TABLE IF NOT EXISTS chat_messages (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id         INTEGER NOT NULL REFERENCES conversations(id),
    role                    TEXT    NOT NULL,
    content                 TEXT    NOT NULL,
    confidence_level        TEXT    NOT NULL DEFAULT 'unavailable',
    confidence_reasons_json TEXT    NOT NULL DEFAULT '[]',
    answerability_kind      TEXT    NOT NULL DEFAULT 'answerable',
    persistence_key         TEXT,
    citations_hash          TEXT,
    citation_count          INTEGER DEFAULT 0,
    created_at              REAL    NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_persistence
    ON chat_messages(conversation_id, persistence_key);

-- Feedback
CREATE TABLE IF NOT EXISTS feedback (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          INTEGER NOT NULL REFERENCES chat_messages(id),
    user_id             INTEGER NOT NULL REFERENCES users(id),
    value               INTEGER NOT NULL,
    comment             TEXT    NOT NULL DEFAULT '',
    kind                TEXT    NOT NULL DEFAULT 'not_useful',
    question_text       TEXT    NOT NULL DEFAULT '',
    question_hash       TEXT    NOT NULL DEFAULT '',
    citation_id         INTEGER,
    suggested_answer    TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT 'submitted',
    created_at          REAL    NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(message_id, user_id)
);

-- Knowledge Base metadata
CREATE TABLE IF NOT EXISTS kb_metadata (
    kb_id       TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    overview    TEXT DEFAULT '',
    user_id     INTEGER,
    owner_id    INTEGER DEFAULT 0,
    scope       TEXT DEFAULT 'private',
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Knowledge Base documents
CREATE TABLE IF NOT EXISTS kb_documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id               TEXT NOT NULL,
    filename            TEXT NOT NULL,
    file_path           TEXT,
    toc                 TEXT DEFAULT '',
    summary             TEXT DEFAULT '',
    chunk_count         INTEGER DEFAULT 0,
    status              TEXT DEFAULT 'pending',
    last_error          TEXT DEFAULT '',
    document_id         INTEGER,
    document_version_id INTEGER,
    added_at            TEXT DEFAULT (datetime('now')),
    UNIQUE(kb_id, filename)
);

-- External data sources
CREATE TABLE IF NOT EXISTS data_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    type            TEXT    NOT NULL,
    config          TEXT    NOT NULL DEFAULT '{}',
    status          TEXT    NOT NULL DEFAULT 'inactive',
    owner_id        TEXT    DEFAULT 'admin',
    last_synced_at  REAL,
    created_at      REAL    NOT NULL DEFAULT (strftime('%s','now'))
);

-- Document permissions
CREATE TABLE IF NOT EXISTS document_permissions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_name            TEXT    NOT NULL,
    kb_id               TEXT    NOT NULL,
    owner_id            INTEGER NOT NULL,
    permission_level    INTEGER DEFAULT 1,
    is_public           BOOLEAN NOT NULL DEFAULT 0,
    protected           BOOLEAN NOT NULL DEFAULT 0,
    scope               TEXT    DEFAULT 'private',
    downloadable        INTEGER DEFAULT 1,
    current_version_id  INTEGER,
    source_type         TEXT    NOT NULL DEFAULT 'upload',
    source_key          TEXT    NOT NULL DEFAULT '',
    updated_at          REAL    NOT NULL DEFAULT 0,
    logical_path        TEXT    NOT NULL DEFAULT '',
    document_type       TEXT    NOT NULL DEFAULT '',
    tags_json           TEXT    NOT NULL DEFAULT '[]',
    subject_user_id     INTEGER,
    metadata_version    INTEGER NOT NULL DEFAULT 1,
    deleted_at          REAL,
    deleted_by          INTEGER,
    restore_until       REAL,
    container_document_id INTEGER,
    container_relative_path TEXT NOT NULL DEFAULT '',
    purge_state         TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    DEFAULT (datetime('now')),
    UNIQUE(doc_name, kb_id)
);
CREATE INDEX IF NOT EXISTS idx_doc_permissions_kb ON document_permissions(kb_id);
CREATE INDEX IF NOT EXISTS idx_doc_permissions_owner ON document_permissions(owner_id);

-- Document shares
CREATE TABLE IF NOT EXISTS document_shares (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    granted_by  INTEGER NOT NULL,
    permission  TEXT DEFAULT 'view',
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(doc_id, user_id),
    FOREIGN KEY (doc_id) REFERENCES document_permissions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_doc_shares_user ON document_shares(user_id);
CREATE INDEX IF NOT EXISTS idx_doc_shares_doc ON document_shares(doc_id);

-- Knowledge Base shares
CREATE TABLE IF NOT EXISTS kb_shares (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id       TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    permission  TEXT DEFAULT 'view',
    granted_by  INTEGER NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(kb_id, user_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_kb_shares_user ON kb_shares(user_id);
CREATE INDEX IF NOT EXISTS idx_kb_shares_kb ON kb_shares(kb_id);

-- Document versions
CREATE TABLE IF NOT EXISTS document_versions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id              INTEGER NOT NULL,
    version_no               INTEGER NOT NULL,
    original_filename        TEXT NOT NULL,
    file_path                TEXT NOT NULL,
    content_hash             TEXT NOT NULL,
    uploaded_by              INTEGER NOT NULL,
    status                   TEXT NOT NULL CHECK(status IN ('staging','indexing','current','historical','recycle_pending','failed','purged')),
    validity_status          TEXT NOT NULL CHECK(validity_status IN ('valid','reported_outdated','confirmed_outdated')) DEFAULT 'valid',
    created_at               REAL NOT NULL DEFAULT (strftime('%s','now')),
    superseded_at            REAL,
    restored_from_version_id INTEGER REFERENCES document_versions(id),
    UNIQUE(document_id, version_no),
    FOREIGN KEY(document_id) REFERENCES document_permissions(id) ON DELETE CASCADE,
    FOREIGN KEY(uploaded_by) REFERENCES users(id)
);

-- Answer citations
CREATE TABLE IF NOT EXISTS answer_citations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_message_id INTEGER NOT NULL,
    document_id          INTEGER NOT NULL,
    document_version_id  INTEGER NOT NULL,
    chunk_index          INTEGER NOT NULL,
    section_title        TEXT NOT NULL,
    text_preview         TEXT NOT NULL,
    rank                 INTEGER NOT NULL,
    is_historical        INTEGER NOT NULL DEFAULT 0,
    state                TEXT NOT NULL DEFAULT 'available',
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY(assistant_message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
);

-- Document comparisons
CREATE TABLE IF NOT EXISTS document_comparisons (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id                INTEGER NOT NULL REFERENCES users(id),
    question                TEXT NOT NULL,
    dimensions_json         TEXT NOT NULL,
    document_snapshot_json  TEXT NOT NULL,
    partial                 INTEGER NOT NULL,
    failures_json           TEXT NOT NULL,
    created_at              REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS comparison_cells (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id   INTEGER NOT NULL REFERENCES document_comparisons(id) ON DELETE CASCADE,
    dimension       TEXT NOT NULL,
    document_id     INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('supported', 'insufficient_evidence')),
    text            TEXT NOT NULL,
    UNIQUE(comparison_id, dimension, document_id)
);

CREATE TABLE IF NOT EXISTS comparison_citations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id             INTEGER NOT NULL REFERENCES comparison_cells(id) ON DELETE CASCADE,
    document_id         INTEGER NOT NULL,
    document_version_id INTEGER NOT NULL,
    chunk_index         INTEGER NOT NULL,
    section_title       TEXT NOT NULL,
    text_preview        TEXT NOT NULL,
    rank                INTEGER NOT NULL
);

-- Vector flag repairs
CREATE TABLE IF NOT EXISTS vector_flag_repairs (
    document_id INTEGER NOT NULL REFERENCES document_permissions(id) ON DELETE CASCADE,
    version_id  INTEGER NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY(document_id, version_id)
);

-- Knowledge tasks and events
CREATE TABLE IF NOT EXISTS knowledge_tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type           TEXT NOT NULL CHECK(task_type IN (
        'outdated', 'not_useful', 'insufficient_evidence', 'evidence_conflict'
    )),
    cluster_key         TEXT NOT NULL,
    active_key          TEXT UNIQUE,
    document_id         INTEGER,
    document_version_id INTEGER,
    assigned_to         INTEGER,
    status              TEXT NOT NULL CHECK(status IN (
        'open', 'in_progress', 'resolved', 'dismissed', 'confirmed_expired'
    )),
    report_count        INTEGER NOT NULL DEFAULT 1,
    resolution_note     TEXT NOT NULL DEFAULT '',
    due_at              REAL NOT NULL,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    resolved_at         REAL
);

CREATE TABLE IF NOT EXISTS knowledge_task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    actor_id    INTEGER NOT NULL,
    old_status  TEXT NOT NULL,
    new_status  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_task_refusal_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    active_key   TEXT NOT NULL,
    user_id      INTEGER NOT NULL,
    question_key TEXT NOT NULL,
    document_id  INTEGER,
    created_at   REAL NOT NULL,
    UNIQUE(active_key, user_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES document_permissions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS knowledge_conflicts (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    active_key                  TEXT UNIQUE,
    subject_key                 TEXT NOT NULL,
    attribute_key               TEXT NOT NULL,
    unit_key                    TEXT NOT NULL,
    scope_key                   TEXT NOT NULL,
    citation_ids_json           TEXT NOT NULL,
    document_ids_json           TEXT NOT NULL,
    document_version_ids_json   TEXT NOT NULL,
    status                      TEXT NOT NULL CHECK(status IN ('open', 'resolved', 'dismissed')),
    resolution                  TEXT,
    resolution_note             TEXT NOT NULL DEFAULT '',
    resolved_by                 INTEGER REFERENCES users(id),
    created_at                  REAL NOT NULL,
    updated_at                  REAL NOT NULL,
    resolved_at                 REAL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_conflicts_status
    ON knowledge_conflicts(status, updated_at);

CREATE TABLE IF NOT EXISTS knowledge_conflict_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_id INTEGER NOT NULL REFERENCES knowledge_conflicts(id) ON DELETE CASCADE,
    actor_id    INTEGER NOT NULL REFERENCES users(id),
    old_status  TEXT NOT NULL,
    new_status  TEXT NOT NULL,
    resolution  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

-- Material lifecycle and indexing jobs
CREATE TABLE IF NOT EXISTS document_index_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id      INTEGER NOT NULL,
    version_id       INTEGER NOT NULL,
    content_hash     TEXT NOT NULL,
    job_type         TEXT NOT NULL DEFAULT 'material_index',
    status           TEXT NOT NULL DEFAULT 'queued',
    payload_hash     TEXT NOT NULL DEFAULT '',
    available_at     REAL NOT NULL,
    lease_owner      TEXT,
    lease_expires_at REAL,
    attempt          INTEGER NOT NULL DEFAULT 0,
    error_code       TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at       REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_document_index_jobs_active
    ON document_index_jobs(document_id, status);

CREATE TABLE IF NOT EXISTS material_archive_manifests (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    container_document_id INTEGER NOT NULL,
    container_version_id  INTEGER NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending_confirmation',
    entry_count           INTEGER NOT NULL DEFAULT 0,
    total_bytes           INTEGER NOT NULL DEFAULT 0,
    reason_code           TEXT NOT NULL DEFAULT '',
    created_by            INTEGER NOT NULL,
    source_path           TEXT NOT NULL DEFAULT '',
    expires_at            REAL NOT NULL DEFAULT 0,
    created_at            REAL NOT NULL DEFAULT (strftime('%s','now')),
    confirmed_at          REAL
);

CREATE TABLE IF NOT EXISTS material_archive_entries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_id          INTEGER NOT NULL REFERENCES material_archive_manifests(id) ON DELETE CASCADE,
    relative_path        TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    byte_size            INTEGER NOT NULL,
    document_id          INTEGER,
    status               TEXT NOT NULL DEFAULT 'staged',
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS material_organization_proposals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by           INTEGER NOT NULL,
    status               TEXT NOT NULL DEFAULT 'proposed',
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now')),
    applied_at           REAL
);

CREATE TABLE IF NOT EXISTS material_organization_items (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id          INTEGER NOT NULL REFERENCES material_organization_proposals(id) ON DELETE CASCADE,
    document_id          INTEGER NOT NULL,
    proposed_path        TEXT NOT NULL,
    proposed_tags_json   TEXT NOT NULL DEFAULT '[]',
    proposed_type        TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'pending',
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS material_organization_batches (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by           INTEGER NOT NULL,
    item_count           INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'committed',
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS material_organization_batch_items (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             INTEGER NOT NULL REFERENCES material_organization_batches(id) ON DELETE CASCADE,
    document_id          INTEGER NOT NULL,
    version_id           INTEGER NOT NULL,
    source_path          TEXT NOT NULL,
    target_path          TEXT NOT NULL,
    tags_json            TEXT NOT NULL DEFAULT '[]',
    document_type        TEXT NOT NULL DEFAULT '',
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS material_archive_import_results (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_id          INTEGER NOT NULL REFERENCES material_archive_manifests(id) ON DELETE CASCADE,
    imported_count       INTEGER NOT NULL DEFAULT 0,
    skipped_count        INTEGER NOT NULL DEFAULT 0,
    failed_count         INTEGER NOT NULL DEFAULT 0,
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS material_unit_fingerprints (
    document_id          INTEGER NOT NULL,
    version_id           INTEGER NOT NULL,
    unit_key             TEXT NOT NULL,
    fingerprint          TEXT NOT NULL,
    created_at           REAL NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY(document_id, version_id, unit_key)
);
CREATE INDEX IF NOT EXISTS idx_unit_fp_doc
    ON material_unit_fingerprints(document_id, version_id);

CREATE TABLE IF NOT EXISTS material_audit_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id          INTEGER NOT NULL,
    actor_id             INTEGER NOT NULL,
    action               TEXT NOT NULL,
    created_at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_material_audit_doc
    ON material_audit_log(document_id);
