"""Contract tests for durable KB lifecycle state and synchronous admission."""

from __future__ import annotations

import asyncio
import contextvars
import multiprocessing
import sqlite3
import threading

import pytest

from rag.kb_lifecycle import (
    KBBusy,
    KBDeleted,
    KBDeleteIncomplete,
    KBDeleting,
    KBLifecycleError,
    KBUnknown,
    assert_kb_available,
    assert_kb_available_in_tx,
    kb_mutation,
)
from rag.kb_lifecycle_http import lifecycle_http_error
from rag.user_db import UserDB


def test_userdb_adds_lifecycle_table_without_rewriting_legacy_rows(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE kb_metadata (kb_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "description TEXT DEFAULT '', overview TEXT DEFAULT '', user_id INTEGER, "
            "owner_id INTEGER DEFAULT 0, scope TEXT DEFAULT 'private', created_at TEXT)"
        )
        conn.execute("INSERT INTO kb_metadata (kb_id, name, user_id, owner_id) VALUES ('legacy', 'Legacy KB', 4, 4)")

    db = UserDB(path)
    try:
        row = db.get_kb_metadata("legacy")
        assert row["name"] == "Legacy KB"
        assert row["owner_id"] == 4
        assert db.get_kb_lifecycle("legacy") is None
        columns = {row["name"] for row in db._conn.execute("PRAGMA table_info(kb_lifecycle)")}
        assert columns == {
            "kb_id", "state", "delete_token", "generation", "requested_at",
            "deleted_at", "last_error_code",
        }
        assert list(db._conn.execute("PRAGMA foreign_key_list(kb_lifecycle)")) == []
    finally:
        db.close()


def test_delete_tombstone_is_independent_and_blocks_future_writes(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("docs", "Docs", owner_id=7)
    try:
        with kb_mutation(db, "docs", mode="delete") as permit:
            token = permit.begin_delete()
        row = db.get_kb_lifecycle("docs")
        assert row["state"] == "deleting"
        assert row["generation"] == 1
        assert row["delete_token"] == token
        assert db.get_kb_metadata("docs")["name"] == "Docs"
        assert_kb_available(db, "untracked")
        assert_kb_available(db, "docs", allow_deleting=True)
        with pytest.raises(KBDeleting), kb_mutation(db, "docs"):
            pass
        with pytest.raises(KBDeleting):
            assert_kb_available(db, "docs")
    finally:
        db.close()


def test_unknown_or_system_kb_cannot_create_delete_intent(tmp_path):
    db = UserDB(tmp_path / "users.db")
    try:
        with pytest.raises(KBUnknown), kb_mutation(db, "missing", mode="delete") as permit:
            permit.begin_delete()
        assert db.get_kb_lifecycle("missing") is None
        with pytest.raises(KBUnknown), kb_mutation(db, "rag_docs", mode="delete") as permit:
            permit.begin_delete()
        assert db.get_kb_lifecycle("rag_docs") is None
    finally:
        db.close()


def test_failed_delete_keeps_retryable_intent_and_token(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("docs", "Docs", owner_id=7)
    try:
        with kb_mutation(db, "docs", mode="delete") as permit:
            original = permit.begin_delete()
            permit.fail_delete("vector_cleanup_failed")
        assert db.get_kb_lifecycle("docs")["last_error_code"] == "vector_cleanup_failed"
        with kb_mutation(db, "docs", mode="delete") as retry:
            assert retry.begin_delete() == original
            retry.finish_delete(lambda conn: conn.execute("DELETE FROM kb_metadata WHERE kb_id='docs'"))
        row = db.get_kb_lifecycle("docs")
        assert row["state"] == "deleted"
        assert row["deleted_at"] is not None
        assert db.get_kb_metadata("docs") is None
        with pytest.raises(KBDeleted):
            assert_kb_available(db, "docs")
    finally:
        db.close()


def test_finish_delete_rolls_back_cleanup_and_deleted_transition_together(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("docs", "Docs", owner_id=7)
    try:
        with kb_mutation(db, "docs", mode="delete") as permit:
            permit.begin_delete()
            with pytest.raises(RuntimeError, match="cleanup failed"):
                permit.finish_delete(
                    lambda conn: (conn.execute("DELETE FROM kb_metadata WHERE kb_id='docs'"),
                                  (_ for _ in ()).throw(RuntimeError("cleanup failed")))
                )
        assert db.get_kb_lifecycle("docs")["state"] == "deleting"
        assert db.get_kb_metadata("docs")["name"] == "Docs"
    finally:
        db.close()


def test_delete_completion_rejects_a_replaced_token(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("docs", "Docs", owner_id=7)
    try:
        with kb_mutation(db, "docs", mode="delete") as permit:
            permit.begin_delete()
            db._conn.execute("UPDATE kb_lifecycle SET delete_token='other' WHERE kb_id='docs'")
            db._conn.commit()
            with pytest.raises(KBDeleteIncomplete):
                permit.finish_delete(lambda conn: conn.execute("DELETE FROM kb_metadata WHERE kb_id='docs'"))
        assert db.get_kb_metadata("docs")["name"] == "Docs"
    finally:
        db.close()


def test_delete_permit_cannot_finish_after_its_admission_exits(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("docs", "Docs", owner_id=7)
    try:
        with kb_mutation(db, "docs", mode="delete") as permit:
            permit.begin_delete()
        with pytest.raises(KBLifecycleError):
            permit.finish_delete(lambda conn: conn.execute("DELETE FROM kb_metadata WHERE kb_id='docs'"))
        assert db.get_kb_lifecycle("docs")["state"] == "deleting"
        assert db.get_kb_metadata("docs")["name"] == "Docs"
    finally:
        db.close()


def test_copied_context_cannot_reuse_a_permit_after_admission_exits(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("docs", "Docs", owner_id=7)
    try:
        with kb_mutation(db, "docs", mode="delete") as permit:
            permit.begin_delete()
            copied_context = contextvars.copy_context()
        with pytest.raises(KBLifecycleError):
            copied_context.run(
                permit.finish_delete,
                lambda conn: conn.execute("DELETE FROM kb_metadata WHERE kb_id='docs'"),
            )
        assert db.get_kb_lifecycle("docs")["state"] == "deleting"
        assert db.get_kb_metadata("docs")["name"] == "Docs"
    finally:
        db.close()


def test_delete_permit_cannot_be_used_from_another_thread(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("docs", "Docs", owner_id=7)
    try:
        with kb_mutation(db, "docs", mode="delete") as permit:
            permit.begin_delete()
            outcome = []

            def finish_from_another_thread():
                try:
                    permit.finish_delete(
                        lambda conn: conn.execute("DELETE FROM kb_metadata WHERE kb_id='docs'")
                    )
                except BaseException as error:
                    outcome.append(error)
                else:
                    outcome.append("finished")

            worker = threading.Thread(target=finish_from_another_thread)
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert isinstance(outcome[0], KBLifecycleError)
            assert db.get_kb_lifecycle("docs")["state"] == "deleting"
            assert db.get_kb_metadata("docs")["name"] == "Docs"
    finally:
        db.close()


def test_tombstone_check_inside_transaction_does_not_open_new_gate(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("docs", "Docs", owner_id=7)
    try:
        with kb_mutation(db, "docs", mode="delete") as permit:
            permit.begin_delete()
            with db.locked_transaction() as conn, pytest.raises(KBDeleting):
                assert_kb_available_in_tx(conn, "docs")
    finally:
        db.close()


def test_same_kb_admission_is_nonblocking_and_different_kbs_are_independent(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("one", "One", owner_id=7)
    db.create_kb_metadata("two", "Two", owner_id=7)
    try:
        with kb_mutation(db, "one") as permit:
            with kb_mutation(db, "one") as nested:
                assert nested is permit
            observed = []
            ready = threading.Event()

            def contend():
                ready.set()
                try:
                    with kb_mutation(db, "one"):
                        observed.append("acquired")
                except KBBusy:
                    observed.append("busy")

            worker = threading.Thread(target=contend)
            worker.start()
            assert ready.wait(timeout=5)
            worker.join(timeout=5)
            assert observed == ["busy"]
            with kb_mutation(db, "two"):
                assert_kb_available(db, "two")
    finally:
        db.close()


def test_mutation_gate_is_only_acquired_in_a_sync_worker(tmp_path):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("docs", "Docs", owner_id=7)

    async def attempt():
        with pytest.raises(Exception) as error, kb_mutation(db, "docs"):
            pass
        assert error.value.code == "kb_mutation_requires_sync_worker"

    try:
        asyncio.run(attempt())
    finally:
        db.close()


def test_http_mapping_keeps_machine_code_in_header(tmp_path):
    from fastapi import HTTPException

    from rag.kb_lifecycle import KBDeleteIncomplete

    error = lifecycle_http_error(KBDeleteIncomplete())
    assert isinstance(error, HTTPException)
    assert error.status_code == 503
    assert error.headers["X-KB-Lifecycle-Error"] == "knowledge_base_delete_incomplete"
    assert isinstance(error.detail, str)


def _try_cross_process_admission(db_path: str, result_queue) -> None:
    db = UserDB(db_path)
    try:
        try:
            with kb_mutation(db, "docs"):
                result_queue.put("acquired")
        except KBBusy:
            result_queue.put("busy")
    finally:
        db.close()


def test_file_admission_is_shared_between_processes(tmp_path):
    db_path = tmp_path / "users.db"
    db = UserDB(db_path)
    db.create_kb_metadata("docs", "Docs", owner_id=7)
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(target=_try_cross_process_admission, args=(str(db_path), result_queue))
    try:
        with kb_mutation(db, "docs"):
            process.start()
            assert result_queue.get(timeout=20) == "busy"
            process.join(timeout=20)
            assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        db.close()
