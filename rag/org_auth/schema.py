from __future__ import annotations

import sqlite3


def install_org_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS org_units (
        code TEXT PRIMARY KEY, parent_code TEXT REFERENCES org_units(code),
        name TEXT NOT NULL, kind TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        seed_key TEXT UNIQUE
    );
    CREATE INDEX IF NOT EXISTS idx_org_units_parent ON org_units(parent_code);
    CREATE TABLE IF NOT EXISTS org_people (
        person_code TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
        display_name TEXT NOT NULL, job_title TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        security_version INTEGER NOT NULL DEFAULT 1, seed_key TEXT UNIQUE
    );
    CREATE TABLE IF NOT EXISTS org_memberships (
        person_code TEXT NOT NULL REFERENCES org_people(person_code) ON DELETE CASCADE,
        org_code TEXT NOT NULL REFERENCES org_units(code), is_primary INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(person_code, org_code)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_org_one_primary
        ON org_memberships(person_code) WHERE is_primary = 1;
    CREATE TABLE IF NOT EXISTS org_manager_scopes (
        person_code TEXT NOT NULL REFERENCES org_people(person_code) ON DELETE CASCADE,
        org_code TEXT NOT NULL REFERENCES org_units(code), PRIMARY KEY(person_code, org_code)
    );
    CREATE TABLE IF NOT EXISTS org_sensitive_qualifications (
        person_code TEXT NOT NULL REFERENCES org_people(person_code) ON DELETE CASCADE,
        sensitivity TEXT NOT NULL CHECK(sensitivity IN ('finance','hr','customer')),
        PRIMARY KEY(person_code, sensitivity)
    );
    CREATE TABLE IF NOT EXISTS kb_org_policies (
        kb_id TEXT PRIMARY KEY, org_code TEXT REFERENCES org_units(code),
        kb_type TEXT NOT NULL CHECK(kb_type IN ('company_public','organization','sensitive','unassigned')),
        sensitivity TEXT CHECK(sensitivity IS NULL OR sensitivity IN ('finance','hr','customer')),
        active INTEGER NOT NULL DEFAULT 1, seed_key TEXT UNIQUE,
        CHECK((kb_type = 'sensitive' AND sensitivity IS NOT NULL) OR (kb_type != 'sensitive' AND sensitivity IS NULL))
    );
    CREATE TABLE IF NOT EXISTS kb_org_grants (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kb_id TEXT NOT NULL REFERENCES kb_org_policies(kb_id) ON DELETE CASCADE,
        subject_type TEXT NOT NULL CHECK(subject_type IN ('user','org')),
        subject_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('read','manage')),
        effect TEXT NOT NULL CHECK(effect IN ('allow','deny')), active INTEGER NOT NULL DEFAULT 1,
        granted_by INTEGER NOT NULL REFERENCES users(id), created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
        UNIQUE(kb_id, subject_type, subject_id, role, effect)
    );
    CREATE TABLE IF NOT EXISTS org_audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT NOT NULL,
        target_type TEXT NOT NULL, target_id TEXT NOT NULL, result TEXT NOT NULL,
        reason TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
    );
    CREATE TABLE IF NOT EXISTS org_seed_state (
        seed_key TEXT PRIMARY KEY, seed_version INTEGER NOT NULL, status TEXT NOT NULL,
        updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
    );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(org_units)")}
    if "seed_key" not in columns:
        conn.execute("ALTER TABLE org_units ADD COLUMN seed_key TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_org_units_seed_key ON org_units(seed_key)")
