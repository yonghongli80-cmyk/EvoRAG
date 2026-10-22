"""Legacy SQL-file migration manager for SQLite (not used at runtime).

.. note::
   **Dead code, annotated 2026-10 (D observation).**  Application startup runs
   :func:`rag.db_migrations.run_migrations` over
   :data:`rag.db_migrations.USER_DB_MIGRATIONS`; nothing in ``rag/`` imports this
   module.  It survives only because ``rag.db`` re-exports
   :class:`Migrator`/:func:`run_migrations` for external scripts, and its own
   unit tests in ``tests/test_db_connection.py`` exercise the file-based path.

   Do not add application migrations here: add them to
   ``rag.db_migrations.USER_DB_MIGRATIONS`` (or ``rag/db/migrations/*.sql`` if an
   operator script still calls this manager).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rag.db.connection import ConnectionManager

_DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Migrator:
    """Idempotent SQL-file schema migrator tracking files in ``schema_migrations``.

    Legacy: kept for operator scripts and existing tests only.  The runtime
    schema runner is :func:`rag.db_migrations.run_migrations`.
    """

    def __init__(
        self,
        target: ConnectionManager | sqlite3.Connection,
        migrations_dir: str | Path | None = None,
    ) -> None:
        self.target = target
        self.migrations_dir = Path(migrations_dir) if migrations_dir else _DEFAULT_MIGRATIONS_DIR

    def _get_connection(self) -> tuple[sqlite3.Connection, bool]:
        """Return (conn, is_managed). If is_managed is True, caller does not own the write lock."""
        if isinstance(self.target, ConnectionManager):
            return self.target.get_write_connection(), True
        return self.target, False

    def ensure_migration_table(self, conn: sqlite3.Connection) -> None:
        """Create schema_migrations tracking table if it does not exist."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()

    def get_applied_versions(self, conn: sqlite3.Connection) -> set[str]:
        """Fetch all recorded migration version keys."""
        self.ensure_migration_table(conn)
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        applied = set()
        for r in rows:
            val = r[0] if isinstance(r, (tuple, sqlite3.Row)) else r["version"]
            applied.add(str(val))
            # Also register prefix like "001" if stored as "001_initial_schema"
            if "_" in str(val):
                applied.add(str(val).split("_")[0])
        return applied

    def get_migration_files(self) -> list[Path]:
        """Discover and sort available .sql migration scripts."""
        if not self.migrations_dir.exists():
            return []
        files = [p for p in self.migrations_dir.glob("*.sql") if p.is_file()]
        return sorted(files, key=lambda p: p.name)

    def apply_pending(self) -> list[str]:
        """Run all pending migration scripts in order and record them idempotently."""
        conn, is_mgr = self._get_connection()

        if isinstance(self.target, ConnectionManager):
            with self.target.write_connection():
                return self._execute_pending(conn)
        else:
            return self._execute_pending(conn)

    def _execute_pending(self, conn: sqlite3.Connection) -> list[str]:
        applied = self.get_applied_versions(conn)
        migration_files = self.get_migration_files()
        newly_applied: list[str] = []

        for file_path in migration_files:
            version_key = file_path.stem
            version_prefix = version_key.split("_")[0]
            if version_key in applied or version_prefix in applied:
                continue

            sql_script = file_path.read_text(encoding="utf-8")
            # Execute migration DDL
            conn.executescript(sql_script)

            # Record version
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                (version_key,),
            )
            conn.commit()

            applied.add(version_key)
            applied.add(version_prefix)
            newly_applied.append(version_key)

        return newly_applied


def run_migrations(
    target: ConnectionManager | sqlite3.Connection,
    migrations_dir: str | Path | None = None,
) -> list[str]:
    """Execute pending schema migrations idempotently and return applied versions."""
    migrator = Migrator(target, migrations_dir)
    return migrator.apply_pending()
