import sqlite3

from rag.db_migrations import run_migrations


def test_migration_adds_data_source_owner_id_and_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.execute("CREATE TABLE data_sources (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO data_sources (name) VALUES ('legacy')")
    conn.commit()

    assert run_migrations(conn) == 2
    assert conn.execute("SELECT owner_id FROM data_sources").fetchone()[0] == "admin"
    assert run_migrations(conn) == 2
