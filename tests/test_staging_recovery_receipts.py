import sqlite3

import pytest

from rag.db_migrations import USER_DB_MIGRATIONS, run_migrations
from rag.user_db import UserDB


def _receipt_table(conn):
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='material_recovery_receipts'"
    ).fetchone()


def test_real_user_db_installs_v6_and_reopening_preserves_receipts(tmp_path):
    db_path = tmp_path / "fresh-user.db"
    db = UserDB(db_path)
    conn = db._conn
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    conn.execute(
        "INSERT INTO material_recovery_receipts "
        "(operation_key, request_hash, actor_user_id, document_id, version_id, receipt_json) "
        "VALUES ('keep', 'h', 1, 2, 3, '{}')"
    )
    conn.commit()
    before = conn.execute("SELECT * FROM material_recovery_receipts").fetchall()
    db.close()

    reopened = UserDB(db_path)
    assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert reopened._conn.execute("SELECT * FROM material_recovery_receipts").fetchall() == before
    reopened.close()


def test_v5_upgrade_installs_v6_and_migration_rerun_is_idempotent(tmp_path):
    db_path = tmp_path / "v5-user.db"
    db = UserDB(db_path)
    conn = db._conn
    conn.execute("DROP TABLE material_recovery_receipts")
    conn.execute("PRAGMA user_version = 5")
    conn.execute("INSERT INTO users (username, salt, password) VALUES ('kept', 'salt', 'hash')")
    conn.commit()
    db.close()

    upgraded = UserDB(db_path)
    conn = upgraded._conn
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert conn.execute("SELECT username FROM users WHERE username='kept'").fetchone()[0] == 'kept'
    assert run_migrations(conn) == 6
    assert conn.execute("SELECT username FROM users WHERE username='kept'").fetchone()[0] == 'kept'
    upgraded.close()


def test_v6_migration_has_global_unique_key_and_no_document_delete_cascade(tmp_path):
    db_path = tmp_path / "schema.db"
    db = UserDB(db_path)
    conn = db._conn
    conn.execute(
        "INSERT INTO material_recovery_receipts "
        "(operation_key, request_hash, actor_user_id, document_id, version_id, receipt_json) "
        "VALUES ('one', 'h', 1, 2, 3, '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO material_recovery_receipts "
            "(operation_key, request_hash, actor_user_id, document_id, version_id, receipt_json) "
            "VALUES ('one', 'h2', 4, 5, 6, '{}')"
        )
    conn.rollback()
    assert "FOREIGN KEY" not in (_receipt_table(conn)[0] or "").upper()
    db.close()


def _repo_db(conn):
    conn.executescript(
        """
        CREATE TABLE document_versions (
          id INTEGER PRIMARY KEY, file_path TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE material_archive_manifests (
          id INTEGER PRIMARY KEY, source_path TEXT NOT NULL, status TEXT NOT NULL
        );
        INSERT INTO document_versions VALUES (10, 'old.bin', 'staging');
        INSERT INTO material_archive_manifests VALUES (20, 'archive.old', 'pending_confirmation');
        """
    )
    run_migrations(conn, tuple(migration for migration in USER_DB_MIGRATIONS if migration[0] == 6))


def test_repo_helpers_require_caller_transaction_and_perform_cas(tmp_path):
    from rag.repositories.staging_recovery_repo import (
        RecoveryReceiptConflict,
        cas_recovery_paths,
        get_recovery_receipt,
        insert_recovery_receipt,
    )

    conn = sqlite3.connect(tmp_path / "repo.db")
    _repo_db(conn)
    with pytest.raises(RuntimeError):
        insert_recovery_receipt(
            conn,
            operation_key="k",
            request_hash="h",
            actor_user_id=1,
            document_id=2,
            version_id=10,
            receipt={"ok": True},
        )
    with pytest.raises(RuntimeError):
        get_recovery_receipt(conn, "k")
    conn.execute("BEGIN IMMEDIATE")
    cas_recovery_paths(
        conn,
        version_id=10,
        expected_version_path="old.bin",
        destination_path="new.bin",
    )
    insert_recovery_receipt(
        conn,
        operation_key="k",
        request_hash="h",
        actor_user_id=1,
        document_id=2,
        version_id=10,
        receipt={"ok": True},
    )
    assert get_recovery_receipt(conn, "k")["request_hash"] == "h"
    from rag.repositories.staging_recovery_repo import insert_recovery_receipt

    with pytest.raises(RecoveryReceiptConflict):
        insert_recovery_receipt(
            conn,
            operation_key="k",
            request_hash="other",
            actor_user_id=1,
            document_id=2,
            version_id=10,
            receipt={"ok": False},
        )
    with pytest.raises(RecoveryReceiptConflict):
        cas_recovery_paths(
            conn,
            version_id=10,
            expected_version_path="old.bin",
            destination_path="other.bin",
        )
    conn.rollback()
    assert conn.execute("SELECT file_path FROM document_versions WHERE id=10").fetchone()[0] == "old.bin"
    conn.execute("BEGIN")
    assert get_recovery_receipt(conn, "k") is None
    conn.rollback()
    conn.close()


def test_deferred_commit_failure_rolls_back_metadata_and_receipt_together(tmp_path):
    from rag.repositories.staging_recovery_repo import cas_recovery_paths, insert_recovery_receipt

    conn = sqlite3.connect(tmp_path / "rollback.db")
    conn.execute("PRAGMA foreign_keys=ON")
    _repo_db(conn)
    conn.executescript(
        "CREATE TABLE deferred_parent(id INTEGER PRIMARY KEY);"
        "CREATE TABLE deferred_child(parent_id INTEGER, FOREIGN KEY(parent_id) "
        "REFERENCES deferred_parent(id) DEFERRABLE INITIALLY DEFERRED);"
    )
    conn.execute("BEGIN IMMEDIATE")
    cas_recovery_paths(
        conn, version_id=10, expected_version_path="old.bin", destination_path="new.bin"
    )
    insert_recovery_receipt(
        conn,
        operation_key="rollback",
        request_hash="h",
        actor_user_id=1,
        document_id=2,
        version_id=10,
        receipt={"ok": True},
    )
    conn.execute("INSERT INTO deferred_child VALUES (99)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.commit()
    conn.rollback()
    assert conn.execute("SELECT file_path FROM document_versions WHERE id=10").fetchone()[0] == "old.bin"
    assert conn.execute(
        "SELECT 1 FROM material_recovery_receipts WHERE operation_key='rollback'"
    ).fetchone() is None
    conn.close()
