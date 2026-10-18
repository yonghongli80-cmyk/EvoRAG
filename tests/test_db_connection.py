"""Tests for SQLite WAL mode ConnectionManager and read/write separation."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from rag.db.connection import ConnectionManager


@pytest.fixture()
def db_path(tmp_path):
    """Provide a path to a temporary SQLite database file."""
    return str(tmp_path / "wal_test.db")


@pytest.fixture()
def conn_mgr(db_path):
    """Provide a ConnectionManager instance backed by a temporary file."""
    mgr = ConnectionManager(db_path, busy_timeout_ms=5000)
    # Seed a simple test table for reading
    with mgr.write_connection() as conn:
        conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO test_data (value) VALUES ('initial')")
        conn.commit()
    yield mgr
    mgr.close()


def test_connection_manager_init_wal(conn_mgr):
    """Test that ConnectionManager properly initializes WAL mode and busy timeout."""
    with conn_mgr.write_connection() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000

    with conn_mgr.read_connection() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        query_only = conn.execute("PRAGMA query_only").fetchone()[0]
        assert query_only == 1


def test_read_connection_query_only(conn_mgr):
    """Test that read connections can SELECT but fail on write operations."""
    with conn_mgr.read_connection() as conn:
        rows = conn.execute("SELECT value FROM test_data WHERE id = 1").fetchall()
        assert len(rows) == 1
        assert rows[0]["value"] == "initial"

        # Attempting an INSERT on a query_only connection must raise OperationalError
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO test_data (value) VALUES ('illegal')")


def test_thread_local_read_connections(conn_mgr):
    """Test that each thread caches its own read connection and reuses it."""
    conn_main_1 = conn_mgr.get_read_connection()
    conn_main_2 = conn_mgr.get_read_connection()
    assert conn_main_1 is conn_main_2

    worker_conns = []

    def worker():
        c1 = conn_mgr.get_read_connection()
        c2 = conn_mgr.get_read_connection()
        assert c1 is c2
        worker_conns.append(c1)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert len(worker_conns) == 1
    assert worker_conns[0] is not conn_main_1


def test_concurrent_multi_thread_reads(conn_mgr):
    """Test that multiple threads can read concurrently without contention."""
    num_threads = 8
    reads_per_thread = 25
    errors = []

    def reader_task(thread_idx: int):
        try:
            for _ in range(reads_per_thread):
                with conn_mgr.read_cursor() as cur:
                    cur.execute("SELECT value FROM test_data WHERE id = 1")
                    row = cur.fetchone()
                    assert row[0] == "initial"
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(reader_task, i) for i in range(num_threads)]
        for f in futures:
            f.result()

    assert not errors


def test_reads_do_not_block_and_are_not_blocked_by_writes(conn_mgr):
    """Verify that a reading thread is not blocked by an in-flight write transaction."""
    write_holding_event = threading.Event()
    read_completed_event = threading.Event()
    results = {}

    def writer_worker():
        with conn_mgr.transaction(immediate=True) as conn:
            conn.execute("INSERT INTO test_data (value) VALUES ('written_in_tx')")
            write_holding_event.set()
            # Hold the write lock for 300ms
            time.sleep(0.3)
        results["writer_finished_at"] = time.time()

    def reader_worker():
        # Wait until writer has acquired write lock and began transaction
        write_holding_event.wait(timeout=2.0)
        start_read = time.time()
        with conn_mgr.read_connection() as rconn:
            rows = rconn.execute("SELECT COUNT(*) FROM test_data").fetchone()
            # In WAL mode, uncommitted changes from writer are not visible to readers
            results["read_count"] = rows[0]
        results["reader_finished_at"] = time.time()
        results["read_duration"] = results["reader_finished_at"] - start_read
        read_completed_event.set()

    writer_thread = threading.Thread(target=writer_worker)
    reader_thread = threading.Thread(target=reader_worker)

    writer_thread.start()
    reader_thread.start()

    writer_thread.join(timeout=3.0)
    reader_thread.join(timeout=3.0)

    # Reader should see only committed data (1 row, not 2)
    assert results["read_count"] == 1
    # Reader finished BEFORE the writer released its transaction!
    assert results["reader_finished_at"] < results["writer_finished_at"]
    # Reading should be fast (< 200ms), not delayed by the 300ms writer sleep
    assert results["read_duration"] < 0.25


def test_transaction_rollback_and_commit(conn_mgr):
    """Test that exceptions in transaction block rollback, while normal exit commits."""
    # 1. Rollback on exception
    with pytest.raises(RuntimeError), conn_mgr.transaction():
        conn_mgr.get_write_connection().execute("INSERT INTO test_data (value) VALUES ('rolled_back')")
        raise RuntimeError("simulated error")

    with conn_mgr.read_connection() as rconn:
        row = rconn.execute("SELECT * FROM test_data WHERE value = 'rolled_back'").fetchone()
        assert row is None

    # 2. Commit on clean exit
    with conn_mgr.transaction():
        conn_mgr.get_write_connection().execute("INSERT INTO test_data (value) VALUES ('committed')")

    with conn_mgr.read_connection() as rconn:
        row = rconn.execute("SELECT * FROM test_data WHERE value = 'committed'").fetchone()
        assert row is not None
        assert row["value"] == "committed"


def test_nested_transaction_reuse_and_rollback(conn_mgr):
    """Test that nested transaction calls seamlessly reuse the outer transaction context."""
    # 1. Successful nested commit
    with conn_mgr.transaction():
        conn_mgr.get_write_connection().execute("INSERT INTO test_data (value) VALUES ('nested_outer')")
        with conn_mgr.transaction():
            conn_mgr.get_write_connection().execute("INSERT INTO test_data (value) VALUES ('nested_inner')")

    with conn_mgr.read_connection() as rconn:
        outer_row = rconn.execute("SELECT * FROM test_data WHERE value = 'nested_outer'").fetchone()
        inner_row = rconn.execute("SELECT * FROM test_data WHERE value = 'nested_inner'").fetchone()
        assert outer_row is not None
        assert inner_row is not None

    # 2. Inner exception rolls back entire nested transaction
    with pytest.raises(ValueError, match="inner error"), conn_mgr.transaction():
        conn_mgr.get_write_connection().execute("INSERT INTO test_data (value) VALUES ('rolled_nested_1')")
        with conn_mgr.transaction():
            conn_mgr.get_write_connection().execute("INSERT INTO test_data (value) VALUES ('rolled_nested_2')")
            raise ValueError("inner error")

    with conn_mgr.read_connection() as rconn:
        r1 = rconn.execute("SELECT * FROM test_data WHERE value = 'rolled_nested_1'").fetchone()
        r2 = rconn.execute("SELECT * FROM test_data WHERE value = 'rolled_nested_2'").fetchone()
        assert r1 is None
        assert r2 is None


def test_close_lifecycle(db_path):
    """Test closing the ConnectionManager closes both write and read connections."""
    mgr = ConnectionManager(db_path)
    # Obtain a read conn in this thread
    _ = mgr.get_read_connection()

    mgr.close()
    assert mgr._closed is True

    with pytest.raises(RuntimeError, match="closed"):
        mgr.get_read_connection()

    with pytest.raises(RuntimeError, match="closed"):
        mgr.get_write_connection()

    with pytest.raises(RuntimeError, match="closed"), mgr.write_connection():
        pass


def test_migrator_applies_all_schemas_idempotently(db_path):
    """Test that Migrator applies all bundled migrations and is idempotent."""
    from rag.db.migrator import Migrator, run_migrations

    mgr = ConnectionManager(db_path)
    migrator = Migrator(mgr)

    # 1. First run: all pending migrations applied
    applied = migrator.apply_pending()
    assert len(applied) == 4
    assert "001_initial_schema" in applied
    assert "002_org_permissions" in applied
    assert "003_spreadsheets" in applied
    assert "004_wiki_and_analysis" in applied

    # Verify tables from each migration exist
    with mgr.read_connection() as rconn:
        tables = {
            r[0]
            for r in rconn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # 001
        assert "users" in tables
        assert "conversations" in tables
        assert "chat_messages" in tables
        assert "document_versions" in tables
        # 002
        assert "org_units" in tables
        assert "org_people" in tables
        # 003
        assert "structured_datasets" in tables
        assert "spreadsheet_edit_sessions" in tables
        # 004
        assert "analysis_cards" in tables
        assert "wiki_pages" in tables
        # Migration tracking table
        assert "schema_migrations" in tables

    # 2. Second run: idempotency check (0 pending migrations)
    second_run = run_migrations(mgr)
    assert second_run == []

    mgr.close()


def test_migrator_with_raw_connection(tmp_path):
    """Test Migrator working directly with standard sqlite3.Connection."""
    from rag.db.migrator import run_migrations

    db_file = str(tmp_path / "raw_conn.db")
    conn = sqlite3.connect(db_file)
    applied = run_migrations(conn)
    assert len(applied) == 4

    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert len(rows) == 4
    conn.close()


def test_migrator_custom_directory(tmp_path):
    """Test Migrator running migrations from a custom directory."""
    from rag.db.migrator import Migrator

    custom_dir = tmp_path / "custom_migrations"
    custom_dir.mkdir()
    (custom_dir / "001_alpha.sql").write_text("CREATE TABLE alpha (id INT);", encoding="utf-8")
    (custom_dir / "002_beta.sql").write_text("CREATE TABLE beta (id INT);", encoding="utf-8")

    db_file = str(tmp_path / "custom.db")
    mgr = ConnectionManager(db_file)
    migrator = Migrator(mgr, migrations_dir=custom_dir)

    applied = migrator.apply_pending()
    assert applied == ["001_alpha", "002_beta"]

    with mgr.read_connection() as rconn:
        tables = {r[0] for r in rconn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "alpha" in tables
        assert "beta" in tables

    # Re-apply is no-op
    assert migrator.apply_pending() == []
    mgr.close()
