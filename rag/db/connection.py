"""SQLite WAL mode connection manager supporting read/write separation."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class ConnectionManager:
    """Thread-safe SQLite connection manager with WAL-mode read-write separation.

    - Write connection: Single shared connection guarded by a threading.Lock,
      configured with WAL journal mode, foreign keys, and busy timeout.
    - Read connections: Cached per-thread via threading.local, configured with
      PRAGMA query_only = ON, WAL mode, foreign keys, and busy timeout. Readers
      never acquire the write lock and execute concurrently without contention.
    """

    def __init__(
        self,
        db_path: str | Path,
        busy_timeout_ms: int = 5000,
        timeout: float = 30.0,
        row_factory: Any = sqlite3.Row,
    ) -> None:
        self.db_path = str(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        self.timeout = timeout
        self.row_factory = row_factory

        if self.db_path != ":memory:":
            parent_dir = Path(self.db_path).parent
            if parent_dir and not parent_dir.exists():
                parent_dir.mkdir(parents=True, exist_ok=True)

        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._all_read_conns: list[sqlite3.Connection] = []
        self._all_conns_lock = threading.Lock()
        self._closed = False

        self._write_conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=self.timeout,
        )
        if self.row_factory is not None:
            self._write_conn.row_factory = self.row_factory
        self._configure_connection(self._write_conn, is_writer=True)

    def _configure_connection(self, conn: sqlite3.Connection, *, is_writer: bool) -> None:
        """Apply PRAGMAs appropriate for writer or reader connections."""
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        if not is_writer:
            conn.execute("PRAGMA query_only = ON")

    def get_write_connection(self) -> sqlite3.Connection:
        """Return the shared write connection.

        Callers should hold ``self.write_lock`` or use ``write_connection()``.
        """
        if self._closed:
            raise RuntimeError("ConnectionManager is closed")
        return self._write_conn

    def get_read_connection(self) -> sqlite3.Connection:
        """Get or initialize the thread-local read-only connection."""
        if self._closed:
            raise RuntimeError("ConnectionManager is closed")

        conn = getattr(self._local, "read_conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=self.timeout,
            )
            if self.row_factory is not None:
                conn.row_factory = self.row_factory
            self._configure_connection(conn, is_writer=False)
            self._local.read_conn = conn
            with self._all_conns_lock:
                self._all_read_conns.append(conn)
        return conn

    @contextmanager
    def read_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager yielding the thread-local read-only connection without holding write lock."""
        conn = self.get_read_connection()
        yield conn

    @contextmanager
    def read_cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager yielding a cursor from the thread-local read connection."""
        conn = self.get_read_connection()
        cursor = conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    @contextmanager
    def write_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager yielding the shared write connection while holding the write lock."""
        if self._closed:
            raise RuntimeError("ConnectionManager is closed")
        with self._write_lock:
            yield self._write_conn

    @contextmanager
    def write_cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager yielding a cursor from the shared write connection while holding the write lock."""
        with self.write_connection() as conn:
            cursor = conn.cursor()
            try:
                yield cursor
            finally:
                cursor.close()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Generator[sqlite3.Connection, None, None]:
        """Execute a block within a write transaction protected by the write lock."""
        if self._closed:
            raise RuntimeError("ConnectionManager is closed")
        with self._write_lock:
            if self._write_conn.in_transaction:
                yield self._write_conn
                return

            if immediate:
                self._write_conn.execute("BEGIN IMMEDIATE")
            else:
                self._write_conn.execute("BEGIN")
            try:
                yield self._write_conn
            except BaseException:
                self._write_conn.execute("ROLLBACK")
                raise
            else:
                self._write_conn.commit()

    def close(self) -> None:
        """Close write connection and all registered read connections."""
        with self._write_lock:
            if self._closed:
                return
            self._closed = True
            with contextlib.suppress(Exception):
                self._write_conn.close()

            with self._all_conns_lock:
                for conn in self._all_read_conns:
                    with contextlib.suppress(Exception):
                        conn.close()
                self._all_read_conns.clear()

    def __enter__(self) -> ConnectionManager:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
