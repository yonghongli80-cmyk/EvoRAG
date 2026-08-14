import sqlite3

from rag.db_migrations import USER_DB_MIGRATIONS, run_migrations


def test_migration_adds_data_source_owner_id_and_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.execute("CREATE TABLE data_sources (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO data_sources (name) VALUES ('legacy')")
    conn.commit()

    owner_id_migrations = tuple(
        migration for migration in USER_DB_MIGRATIONS if migration[0] <= 2
    )

    assert run_migrations(conn, owner_id_migrations) == 2
    assert conn.execute("SELECT owner_id FROM data_sources").fetchone()[0] == "admin"
    assert run_migrations(conn, owner_id_migrations) == 2
