"""Small, ordered SQLite schema migration runner."""

from collections.abc import Iterable
import sqlite3


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
