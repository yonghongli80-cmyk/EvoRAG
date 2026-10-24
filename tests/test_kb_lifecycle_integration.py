"""Cross-process, cancellation, and crash integration for KB lifecycle teardown."""

from __future__ import annotations

import asyncio
import contextvars
import json
import multiprocessing
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from qdrant_client.models import Distance, PointStruct, VectorParams

from rag.kb_lifecycle import KBBusy, KBDeleted, KBLifecycleError, kb_mutation
from rag.knowledge_base import KnowledgeBaseManager
from rag.user_db import UserDB
from rag.vector_store import close_client
from tests.kb_lifecycle_fixtures import (
    crash_coordinator_after_cleanup,
    crash_during_delete,
    start_gate_child,
)


def _fork_inherited_gate_attempt(db_path: str, kb_id: str, result) -> None:
    db = UserDB(db_path)
    try:
        try:
            with kb_mutation(db, kb_id):
                result.put("acquired")
        except KBBusy:
            result.put("busy")
    finally:
        db.close()


def _read_child_line(child: subprocess.Popen[str]) -> dict:
    assert child.stdout is not None
    line = child.stdout.readline()
    assert line, child.stderr.read() if child.stderr is not None else "child exited without output"
    return json.loads(line)


def _release_gate_child(child: subprocess.Popen[str]) -> None:
    assert child.stdin is not None
    child.stdin.write("release\n")
    child.stdin.flush()
    assert child.wait(timeout=15) == 0


def test_portalocker_gate_is_shared_across_processes_and_per_kb(tmp_path):
    db_path = tmp_path / "synthetic-users.db"
    qdrant_path = tmp_path / "synthetic-qdrant"
    versions_root = tmp_path / "versions"
    db = UserDB(db_path)
    db.create_kb_metadata("kb_process_one", "one", 1, "private")
    db.create_kb_metadata("kb_process_two", "two", 1, "private")
    try:
        holder = start_gate_child(
            db_path, qdrant_path, versions_root, "kb_process_one", hold=True
        )
        assert _read_child_line(holder)["state"] == "acquired"

        same = start_gate_child(
            db_path, qdrant_path, versions_root, "kb_process_one", hold=False
        )
        assert _read_child_line(same)["state"] == "busy"
        assert same.wait(timeout=15) == 0

        other = start_gate_child(
            db_path, qdrant_path, versions_root, "kb_process_two", hold=True
        )
        assert _read_child_line(other)["state"] == "acquired"
        _release_gate_child(other)
        _release_gate_child(holder)

        # Same-thread nesting is allowed. A ContextVar copied while the gate is
        # active must not re-enter the public gate API after the owner exits.
        with kb_mutation(db, "kb_process_one"):
            with kb_mutation(db, "kb_process_one") as nested:
                assert nested.kb_id == "kb_process_one"

            task_body_entered = []
            task_threads = []
            owner_thread = threading.get_ident()

            async def inherited_child_task_attempt():
                task_threads.append(threading.get_ident())
                with kb_mutation(db, "kb_process_one"):
                    task_body_entered.append(True)

            async def run_inherited_child_task():
                task = asyncio.create_task(inherited_child_task_attempt())
                try:
                    await task
                except KBLifecycleError as error:
                    return error.code
                return "accepted"

            assert asyncio.run(run_inherited_child_task()) == "kb_mutation_requires_sync_worker"
            assert task_threads == [owner_thread]
            assert task_body_entered == []
            copied_owner_context = contextvars.copy_context()

        with pytest.raises(KBLifecycleError) as inactive:
            copied_owner_context.run(lambda: _run_gate_write(db, "kb_process_one"))
        assert inactive.value.code == "kb_mutation_permit_inactive"
    finally:
        db.close()


def test_forked_process_cannot_reuse_inherited_permit(tmp_path):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("fork start method is unavailable on this platform")

    db = UserDB(tmp_path / "synthetic-users.db")
    db.create_kb_metadata("kb_forked", "forked", 1, "private")
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    child = None
    try:
        with kb_mutation(db, "kb_forked"):
            child = context.Process(
                target=_fork_inherited_gate_attempt,
                args=(str(db.db_path), "kb_forked", results),
            )
            child.start()
            assert results.get(timeout=15) == "busy"
            child.join(timeout=15)
            assert child.exitcode == 0
    finally:
        if child is not None and child.is_alive():
            child.join(timeout=15)
        results.close()
        results.join_thread()
        db.close()


def _run_gate_write(db, kb_id: str) -> None:
    with kb_mutation(db, kb_id):
        return


def test_retained_stale_operation_cannot_resurrect_deleted_kb(
    isolated_api, make_user
):
    owner, headers = make_user("stale-operation-owner")
    db = isolated_api.app.state.user_db
    manager = KnowledgeBaseManager()
    kb_id = manager.create_kb("stale-operation")
    db.create_kb_metadata(kb_id, "stale operation", owner["id"], "private")
    late_file = Path(isolated_api.app.state.kb_files_root) / kb_id / "late.txt"
    entered_gate = []

    def retained_operation():
        with kb_mutation(db, kb_id):
            entered_gate.append(True)
            manager._client.create_collection(
                collection_name=kb_id,
                vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
            )
            late_file.parent.mkdir(parents=True, exist_ok=True)
            late_file.write_text("stale publication", encoding="utf-8")

    try:
        deleted = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert db.get_kb_lifecycle(kb_id)["state"] == "deleted"
        assert not manager._client.collection_exists(kb_id)

        with pytest.raises(KBDeleted):
            retained_operation()

        assert entered_gate == []
        assert not manager._client.collection_exists(kb_id)
        assert not late_file.exists()
    finally:
        close_client()


@pytest.mark.parametrize(
    ("checkpoint", "exit_code"),
    [
        ("deleting_committed", 70),
        ("vectors_removed", 71),
        ("part_files_removed", 72),
        ("before_final_sql", 73),
    ],
)
def test_crashed_delete_releases_process_lock_and_retry_converges(
    tmp_path, checkpoint, exit_code, monkeypatch
):
    from config import settings

    db_path = tmp_path / "synthetic-users.db"
    qdrant_path = tmp_path / "synthetic-qdrant"
    monkeypatch.setattr(settings, "qdrant_path", str(qdrant_path))
    versions_root = tmp_path / "versions"
    kb_id, other_kb = "kb_crash_target", "kb_crash_other"
    db = UserDB(db_path)
    db.create_kb_metadata(kb_id, "target", 1, "private")
    db.create_kb_metadata(other_kb, "other", 1, "private")
    manager = KnowledgeBaseManager()
    manager._client.create_collection(
        collection_name=kb_id,
        vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
    )
    manager._client.create_collection(
        collection_name=other_kb,
        vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
    )
    manager._client.upsert(
        collection_name=kb_id,
        points=[PointStruct(id=1, vector=[0.1] * 1024, payload={"doc_name": "target.txt"})],
    )
    manager._client.upsert(
        collection_name=other_kb,
        points=[PointStruct(id=2, vector=[0.2] * 1024, payload={"doc_name": "other.txt"})],
    )
    files = [tmp_path / "files" / "target-a.txt", tmp_path / "files" / "target-b.txt"]
    for index, path in enumerate(files):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"synthetic file {index}", encoding="utf-8")

    # The crash child must exclusively own the local Qdrant directory.
    close_client()
    try:
        child = crash_during_delete(
            db_path, qdrant_path, versions_root, kb_id, files, checkpoint
        )
        stdout, stderr = child.communicate(timeout=45)
        assert child.returncode == exit_code, stderr
        assert "deleting:" in stdout

        row = db.get_kb_lifecycle(kb_id)
        assert row["state"] == "deleting"
        token = row["delete_token"]
        assert token
        if checkpoint == "part_files_removed":
            assert not files[0].exists() and files[1].exists()
        if checkpoint == "before_final_sql":
            assert not any(path.exists() for path in files)

        # Reopen local Qdrant after child exit. The same lock identity and token
        # are retried; only KB-owned rows, vectors and files are removed.
        manager = KnowledgeBaseManager()
        with kb_mutation(db, kb_id, mode="delete") as permit:
            assert permit.begin_delete() == token
            assert permit.assert_delete_started() is None
            manager.delete_kb(kb_id)
            for path in files:
                path.unlink(missing_ok=True)
            permit.finish_delete(
                lambda conn: conn.execute("DELETE FROM kb_metadata WHERE kb_id = ?", (kb_id,))
            )

        assert db.get_kb_lifecycle(kb_id)["state"] == "deleted"
        assert db.get_kb_metadata(kb_id) is None
        assert not any(path.exists() for path in files)
        assert not manager._client.collection_exists(kb_id)
        assert manager._client.collection_exists(other_kb)
        assert manager._client.count(collection_name=other_kb).count == 1
        with pytest.raises(KBDeleted), kb_mutation(db, kb_id):
            pytest.fail("deleted KB accepted a new mutation")
    finally:
        db.close()
        close_client()


def test_coordinator_crash_retries_scoped_cleanup_without_touching_other_kb(
    isolated_api, make_user
):
    from config import settings
    from rag.wiki.indexer import enqueue_wiki_reindex_job
    from rag.wiki.store import WikiStore

    owner, headers = make_user("coordinator-crash-owner")
    db = isolated_api.app.state.user_db
    kb_id, other_kb = "kb_coordinator_target", "kb_coordinator_other"
    db.create_kb_metadata(kb_id, "Coordinator target", owner["id"], "private")
    db.create_kb_metadata(other_kb, "Coordinator other", owner["id"], "private")

    versions_root = Path(isolated_api.app.state.document_versions_root)
    kb_files_root = Path(isolated_api.app.state.kb_files_root)
    datasets_root = Path(isolated_api.app.state.structured_datasets_root)
    wiki_db_path = Path(settings.wiki_db_path)
    bm25_db_path = Path(settings.bm25_db_path)

    def seed_document(target_kb, filename, body):
        permission_id = db.create_document_permission(filename, target_kb, owner["id"])
        version_path = versions_root / str(permission_id) / "1" / filename
        version_path.parent.mkdir(parents=True, exist_ok=True)
        version_path.write_text(body, encoding="utf-8")
        version_id = db.create_document_version(
            permission_id, filename, str(version_path), f"hash-{filename}", owner["id"]
        )
        db.activate_document_version(permission_id, version_id, target_kb, filename)
        kb_document_id = db.create_kb_document_pending(target_kb, filename)
        kb_file = kb_files_root / target_kb / str(kb_document_id) / filename
        kb_file.parent.mkdir(parents=True, exist_ok=True)
        kb_file.write_text(f"legacy {body}", encoding="utf-8")
        db.finish_kb_document(kb_document_id, str(kb_file), "[]", "synthetic", 1)
        return permission_id, version_id, [version_path, kb_file]

    target_doc_id, target_version_id, target_files = seed_document(
        kb_id, "target.txt", "target body"
    )
    other_doc_id, other_version_id, other_files = seed_document(
        other_kb, "other.txt", "other body"
    )
    target_material_job = db.enqueue_material_job(target_doc_id, target_version_id)
    other_material_job = db.enqueue_material_job(other_doc_id, other_version_id)
    db.insert_material_search_fts(
        target_doc_id, target_version_id, "target.txt", "", "", "[]", "target body"
    )
    db.insert_material_search_fts(
        other_doc_id, other_version_id, "other.txt", "", "", "[]", "other body"
    )
    with db.locked_transaction() as conn:
        conn.executemany(
            "INSERT INTO material_unit_fingerprints(document_id,version_id,unit_key,digest) "
            "VALUES(?,?, 'unit','digest')",
            [(target_doc_id, target_version_id), (other_doc_id, other_version_id)],
        )

    wiki_store = WikiStore(db_path=wiki_db_path)
    target_page = wiki_store.create_page(kb_id, "Target page", "Target wiki body.")
    other_page = wiki_store.create_page(other_kb, "Other page", "Other wiki body.")
    wiki_store.close()
    assert enqueue_wiki_reindex_job(
        db, target_page.id, target_page.current_revision_id, kb_id=kb_id
    )
    assert enqueue_wiki_reindex_job(
        db, other_page.id, other_page.current_revision_id, kb_id=other_kb
    )

    manager = KnowledgeBaseManager()
    for target, point_id, value in ((kb_id, 101, 0.1), (other_kb, 202, 0.2)):
        manager._client.create_collection(
            collection_name=target,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
        )
        manager._client.upsert(
            collection_name=target,
            points=[PointStruct(id=point_id, vector=[value] * 1024, payload={"doc_name": "body.txt"})],
        )

    qdrant_path = Path(settings.qdrant_path)
    db_path = Path(db.db_path)
    close_client()
    child = None
    try:
        child = crash_coordinator_after_cleanup(
            db_path,
            qdrant_path,
            versions_root,
            kb_files_root,
            datasets_root,
            wiki_db_path,
            bm25_db_path,
            kb_id,
            owner["id"],
            74,
        )
        try:
            stdout, stderr = child.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate(timeout=15)
            pytest.fail(f"owned coordinator child timed out; stdout={stdout!r}, stderr={stderr!r}")

        assert child.returncode == 74, stderr
        assert "actual_coordinator_cleanup_complete" in stdout
        lifecycle = db.get_kb_lifecycle(kb_id)
        assert lifecycle["state"] == "deleting"
        retained_token = lifecycle["delete_token"]
        assert retained_token
        assert db.get_kb_metadata(kb_id) is not None
        assert db.get_kb_metadata(other_kb) is not None
        assert all(not path.exists() for path in target_files)
        assert all(path.exists() for path in other_files)
        assert db.get_index_job(target_material_job)["status"] == "cancelled"
        assert db.get_index_job(other_material_job)["status"] == "queued"

        with db.cm.read_connection() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM material_search_fts WHERE document_id = ?", (target_doc_id,)
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM material_search_fts WHERE document_id = ?", (other_doc_id,)
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM material_unit_fingerprints WHERE document_id = ?", (target_doc_id,)
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM material_unit_fingerprints WHERE document_id = ?", (other_doc_id,)
            ).fetchone()[0] == 1

        manager = KnowledgeBaseManager()
        assert not manager._client.collection_exists(kb_id)
        assert manager._client.collection_exists(other_kb)
        assert manager._client.count(collection_name=other_kb).count == 1

        from rag.wiki.api import get_wiki_store

        store = get_wiki_store()
        assert store.list_pages(kb_id=kb_id) == []
        assert [page.id for page in store.list_pages(kb_id=other_kb)] == [other_page.id]
        wiki_jobs = {
            int(row["document_id"]): row["status"]
            for row in db._conn.execute(
                "SELECT document_id,status FROM document_index_jobs WHERE job_scope='wiki'"
            ).fetchall()
        }
        assert wiki_jobs[target_page.id] == "cancelled"
        assert wiki_jobs[other_page.id] == "queued"

        retried = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)
        assert retried.status_code == 200, retried.text
        assert db.get_kb_lifecycle(kb_id)["state"] == "deleted"
        assert db.get_kb_lifecycle(kb_id)["delete_token"] == retained_token
        assert db.get_kb_metadata(kb_id) is None
        assert db.get_kb_metadata(other_kb) is not None
        assert db.get_document_permission("target.txt", kb_id) is None
        assert db.get_document_permission("other.txt", other_kb) is not None
        with db.cm.read_connection() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM material_search_fts WHERE document_id = ?", (target_doc_id,)
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM material_search_fts WHERE document_id = ?", (other_doc_id,)
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM material_unit_fingerprints WHERE document_id = ?", (target_doc_id,)
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM material_unit_fingerprints WHERE document_id = ?", (other_doc_id,)
            ).fetchone()[0] == 1
        assert all(not path.exists() for path in target_files)
        assert all(path.exists() for path in other_files)
        assert not manager._client.collection_exists(kb_id)
        assert manager._client.collection_exists(other_kb)
        assert manager._client.count(collection_name=other_kb).count == 1
        assert [page.id for page in store.list_pages(kb_id=other_kb)] == [other_page.id]
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        close_client()


def test_cancelled_upload_keeps_gate_until_worker_finishes_then_delete_converges(
    isolated_api, make_user, monkeypatch
):
    import rag.embedder
    import rag.routers.knowledge as knowledge

    owner, headers = make_user("cancel-index-owner")
    db = isolated_api.app.state.user_db
    manager = KnowledgeBaseManager()
    kb_id = manager.create_kb("cancel-index")
    db.create_kb_metadata(kb_id, "cancel-index", owner["id"], "private")
    monkeypatch.setattr(rag.embedder, "embed", lambda texts: [[0.1] * 1024 for _ in texts])
    entered = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()

    class BlockingManager:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def add_document(self, *args, **kwargs):
            entered.set()
            if not release.wait(timeout=20):
                raise TimeoutError("test did not release synthetic index worker")
            return self.wrapped.add_document(*args, **kwargs)

    monkeypatch.setattr(knowledge, "KnowledgeBaseManager", lambda: BlockingManager(manager))
    original_ingest = knowledge._ingest_kb_document_sync

    def track_ingest(*args, **kwargs):
        try:
            return original_ingest(*args, **kwargs)
        finally:
            worker_finished.set()

    monkeypatch.setattr(knowledge, "_ingest_kb_document_sync", track_ingest)

    async def exercise_cancel_and_delete():
        transport = httpx.ASGITransport(app=isolated_api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            upload = asyncio.create_task(
                client.post(
                    f"/knowledge-bases/{kb_id}/documents",
                    headers=headers,
                    files={"file": ("cancelled.txt", b"synthetic document body", "text/plain")},
                )
            )
            assert await asyncio.to_thread(entered.wait, 10)
            upload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await upload

            competing_delete = await client.delete(
                f"/knowledge-bases/{kb_id}", headers=headers
            )
            assert competing_delete.status_code == 409
            assert db.get_kb_lifecycle(kb_id) is None
            # The worker may have reserved its document identity before cancellation;
            # the competing delete must not have changed it or tombstoned the KB.
            in_flight = db.get_document_permission("cancelled.txt", kb_id)
            assert in_flight is not None
            assert manager._client.collection_exists(kb_id)

            release.set()
            assert await asyncio.to_thread(worker_finished.wait, 15)
            # The completed worker has published a real local vector and version;
            # a later delete must remove both and leave the permanent tombstone.
            response = await client.delete(f"/knowledge-bases/{kb_id}", headers=headers)
            assert response.status_code == 200, response.text
            assert db.get_kb_lifecycle(kb_id)["state"] == "deleted"
            assert db.get_document_permission("cancelled.txt", kb_id) is None
            assert not manager._client.collection_exists(kb_id)

    try:
        asyncio.run(exercise_cancel_and_delete())
    finally:
        release.set()
        close_client()


def _seed_file_document(db, kb_id: str, owner_id: int, root: Path, name: str) -> Path:
    document_id = db.create_kb_document_pending(kb_id, name)
    path = root / kb_id / str(document_id) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"synthetic {name}", encoding="utf-8")
    db.finish_kb_document(document_id, str(path), "[]", "synthetic", 0)
    return path


@pytest.mark.parametrize("failure", ["vector", "unlink", "final_sql"])
def test_delete_failures_return_503_keep_identity_and_retry_safely(
    isolated_api, make_user, monkeypatch, failure
):
    import rag.kb_lifecycle_cleanup as cleanup
    import rag.routers.knowledge as knowledge

    owner, headers = make_user(f"failure-owner-{failure}")
    db = isolated_api.app.state.user_db
    real_manager = KnowledgeBaseManager()
    kb_id = real_manager.create_kb(f"failure-{failure}")
    other_kb = real_manager.create_kb(f"other-{failure}")
    db.create_kb_metadata(kb_id, "target", owner["id"], "private")
    db.create_kb_metadata(other_kb, "other", owner["id"], "private")
    file_root = Path(isolated_api.app.state.kb_files_root)
    first = _seed_file_document(db, kb_id, owner["id"], file_root, "first.txt")
    second = _seed_file_document(db, kb_id, owner["id"], file_root, "second.txt")
    other_file = _seed_file_document(db, other_kb, owner["id"], file_root, "keep.txt")
    token_before_retry = None

    if failure == "vector":
        class FailingVectorManager:
            def __init__(self):
                self.wrapped = real_manager

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

            def delete_kb(self, _kb_id):
                raise RuntimeError("synthetic vector delete failure")

        monkeypatch.setattr(knowledge, "KnowledgeBaseManager", FailingVectorManager)
    elif failure == "unlink":
        real_delete_files = cleanup.delete_kb_document_files

        def fail_after_one(paths):
            real_delete_files(paths[:1])
            raise OSError("synthetic unlink failure")

        monkeypatch.setattr(cleanup, "delete_kb_document_files", fail_after_one)
    else:
        with db.locked_transaction() as conn:
            conn.execute(
                "CREATE TRIGGER fail_target_metadata_delete BEFORE DELETE ON kb_metadata "
                f"WHEN OLD.kb_id = '{kb_id}' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic final SQL failure'); END"
            )

    try:
        failed = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)
        assert failed.status_code == 503
        row = db.get_kb_lifecycle(kb_id)
        assert row["state"] == "deleting"
        assert row["delete_token"]
        token_before_retry = row["delete_token"]
        assert row["last_error_code"]
        assert db.get_kb_metadata(kb_id) is not None
        assert db.get_kb_metadata(other_kb) is not None
        assert other_file.read_text(encoding="utf-8") == "synthetic keep.txt"

        if failure == "vector":
            assert real_manager._client.collection_exists(kb_id)
            assert first.exists() and second.exists()
        elif failure == "unlink":
            assert not first.exists() and second.exists()
        else:
            assert not real_manager._client.collection_exists(kb_id)
            assert not first.exists() and not second.exists()

        if failure == "vector":
            monkeypatch.setattr(knowledge, "KnowledgeBaseManager", KnowledgeBaseManager)
        elif failure == "unlink":
            monkeypatch.setattr(cleanup, "delete_kb_document_files", real_delete_files)
        if failure == "final_sql":
            with db.locked_transaction() as conn:
                conn.execute("DROP TRIGGER fail_target_metadata_delete")

        retried = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)
        assert retried.status_code == 200, retried.text
        row = db.get_kb_lifecycle(kb_id)
        assert row["state"] == "deleted"
        assert row["delete_token"] == token_before_retry
        assert db.get_kb_metadata(kb_id) is None
        assert not first.exists() and not second.exists()
        assert real_manager._client.collection_exists(other_kb)
        assert other_file.exists()
        with pytest.raises(KBDeleted), kb_mutation(db, kb_id):
            pytest.fail("deleted KB accepted a new mutation")
    finally:
        real_manager._client.close()
