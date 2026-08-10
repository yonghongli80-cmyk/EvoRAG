import sqlite3

from rag.knowledge_tasks import migrate_knowledge_task_types
from rag.user_db import UserDB


def legacy_task_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy-tasks.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE knowledge_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL CHECK(task_type IN ('outdated', 'not_useful')),
            cluster_key TEXT NOT NULL,
            active_key TEXT UNIQUE,
            document_id INTEGER,
            document_version_id INTEGER,
            assigned_to INTEGER,
            status TEXT NOT NULL CHECK(status IN ('open', 'in_progress', 'resolved', 'dismissed', 'confirmed_expired')),
            report_count INTEGER NOT NULL DEFAULT 1,
            resolution_note TEXT NOT NULL DEFAULT '',
            due_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            resolved_at REAL
        );
        CREATE INDEX idx_legacy_tasks_status ON knowledge_tasks(status, due_at);
        CREATE TRIGGER trg_legacy_tasks_after_update
        AFTER UPDATE OF resolution_note ON knowledge_tasks
        BEGIN
            UPDATE knowledge_tasks SET updated_at = NEW.updated_at WHERE id = NEW.id;
        END;
        """
    )
    conn.execute(
        """
        INSERT INTO knowledge_tasks (
            task_type, cluster_key, active_key, status, report_count, due_at, created_at, updated_at
        ) VALUES ('outdated', 'outdated:7', 'outdated:7', 'open', 1, 1, 1, 1)
        """
    )
    conn.commit()
    return conn


def test_current_task_schema_is_already_widened(tmp_path):
    db = UserDB(str(tmp_path / "current-tasks.db"))
    schema = db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_tasks'"
    ).fetchone()["sql"]

    assert "insufficient_evidence" in schema
    assert "evidence_conflict" in schema
    named_schema_objects = db._conn.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type IN ('index', 'trigger')
          AND tbl_name = 'knowledge_tasks'
          AND sql IS NOT NULL
        """
    ).fetchall()
    assert named_schema_objects == []


def test_task_type_migration_preserves_rows_indexes_and_triggers(tmp_path):
    conn = legacy_task_db(tmp_path)
    definitions_before = conn.execute(
        """
        SELECT type, name, sql FROM sqlite_master
        WHERE tbl_name = 'knowledge_tasks' AND type IN ('index', 'trigger') AND sql IS NOT NULL
        ORDER BY type, name
        """
    ).fetchall()

    migrate_knowledge_task_types(conn)

    rows = conn.execute("SELECT task_type FROM knowledge_tasks").fetchall()
    assert [row["task_type"] for row in rows] == ["outdated"]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.execute(
        """
        INSERT INTO knowledge_tasks (
            task_type, cluster_key, status, due_at, created_at, updated_at
        ) VALUES ('insufficient_evidence', 'insufficient:7', 'open', 1, 1, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO knowledge_tasks (
            task_type, cluster_key, status, due_at, created_at, updated_at
        ) VALUES ('evidence_conflict', 'conflict:7', 'open', 1, 1, 1)
        """
    )
    definitions_after = conn.execute(
        """
        SELECT type, name, sql FROM sqlite_master
        WHERE tbl_name = 'knowledge_tasks' AND type IN ('index', 'trigger') AND sql IS NOT NULL
        ORDER BY type, name
        """
    ).fetchall()

    assert definitions_after == definitions_before
