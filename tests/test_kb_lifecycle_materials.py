"""KB lifecycle fences around material APIs, archives, and background jobs."""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest

from rag.kb_lifecycle import KBBusy, KBDeleting, kb_mutation
from rag.materials.formats import build_default_format_registry
from rag.materials.migration import migrate_material_metadata
from rag.materials.models import MaterialLimits
from rag.materials.organizer import OrganizationInstruction, OrganizationService
from rag.materials.recycle import RESTORE_WINDOW_SECONDS, restore_from_recycle, scan_due_purges
from rag.materials.staging import stream_upload_to_stage
from rag.materials.worker import JobResult, MaterialWorker, WorkerServices
from rag.user_db import UserDB


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("child.txt", "archive child")
    return buffer.getvalue()


def test_custom_kb_upload_requires_edit_authorization_even_without_org_policy(isolated_api, make_user):
    owner, _ = make_user("material-owner")
    _other, other_headers = make_user("material-viewer")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("custom-kb", "Custom", owner_id=owner["id"])

    response = isolated_api.post(
        "/materials/upload?kb_id=custom-kb",
        headers=other_headers,
        files={"file": ("restricted.txt", b"private", "text/plain")},
    )

    assert response.status_code == 403
    assert db.get_document_permission("restricted.txt", "custom-kb") is None


def test_custom_kb_staging_is_namespaced_before_document_identity_exists(tmp_path):
    kb_id = "customer-project/knowledge"

    async def upload():
        async def chunks():
            yield b"first "
            yield b"second"

        return await stream_upload_to_stage(
            chunks(),
            "notes.txt",
            "text/plain",
            tmp_path / "versions",
            build_default_format_registry(),
            MaterialLimits(file_bytes=32),
            kb_id=kb_id,
        )

    staged = asyncio.run(upload())
    assert staged.path.parent == (
        tmp_path / "versions" / "staging" / "kb" / hashlib.sha256(kb_id.encode()).hexdigest()
    )
    assert staged.path.read_bytes() == b"first second"


@pytest.mark.parametrize("state", ["deleting", "deleted"])
def test_material_upload_rejects_kb_tombstones(isolated_api, make_user, state):
    owner, headers = make_user(f"upload-{state}")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("locked-kb", "Locked", owner_id=owner["id"])
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO kb_lifecycle (kb_id, state, delete_token, generation, requested_at) "
            "VALUES ('locked-kb', ?, 'deleting-token', 1, 1)",
            (state,),
        )

    response = isolated_api.post(
        "/materials/upload?kb_id=locked-kb",
        headers=headers,
        files={"file": ("blocked.txt", b"must not be promoted", "text/plain")},
    )

    assert response.status_code == (409 if state == "deleting" else 404)
    assert response.headers["X-KB-Lifecycle-Error"] == f"knowledge_base_{state}"
    assert db.get_document_permission("blocked.txt", "locked-kb") is None


@pytest.mark.parametrize("state", ["deleting", "deleted"])
def test_archive_confirmation_rejects_kb_tombstones(isolated_api, make_user, state):
    owner, headers = make_user(f"archive-{state}")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("archive-kb", "Archive KB", owner_id=owner["id"])
    uploaded = isolated_api.post(
        "/materials/upload?kb_id=archive-kb",
        headers=headers,
        files={"file": ("bundle.zip", _zip_bytes(), "application/zip")},
    )
    assert uploaded.status_code == 202
    manifest_id = uploaded.json()["manifest_id"]
    manifest = isolated_api.get(f"/materials/archives/{manifest_id}", headers=headers)
    selected = [entry["id"] for entry in manifest.json()["entries"] if entry["state"] == "approved"]
    container_id = manifest.json()["container_document_id"]
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO kb_lifecycle (kb_id, state, delete_token, generation, requested_at) "
            "VALUES ('archive-kb', ?, 'deleting-token', 1, 1)",
            (state,),
        )

    response = isolated_api.post(
        f"/materials/archives/{manifest_id}/confirm",
        headers=headers,
        json={
            "selected_entry_ids": selected,
            "expected_version": 0,
            "idempotency_key": "confirm-after-tombstone",
        },
    )

    assert response.status_code == (409 if state == "deleting" else 404)
    assert response.headers["X-KB-Lifecycle-Error"] == f"knowledge_base_{state}"
    assert db.get_document_permission_by_id(container_id)["current_version_id"] is None
    assert db.list_container_children(container_id) == []


def _organization_setup(tmp_path):
    db = UserDB(tmp_path / "organization.db")
    actor_id = db.create_user("organizer", "Password-123!")
    db.create_kb_metadata("kb-organizer", "Organizer KB", owner_id=actor_id)
    document_id = db.create_document_permission("report.txt", "kb-organizer", actor_id)
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE document_permissions SET logical_path='before' WHERE id=?", (document_id,)
        )
    actor = {"id": actor_id, "username": "organizer", "is_admin": False}
    service = OrganizationService(
        db,
        generate=lambda _messages, _model: {
            "items": [{"document_id": document_id, "target_path": "after", "confidence": 0.9}]
        },
        candidate_docs=lambda _db, _actor: {
            document_id: db.get_document_permission_by_id(document_id)
        },
    )
    return db, service, actor, document_id


def _set_kb_state(db: UserDB, kb_id: str, state: str):
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO kb_lifecycle (kb_id, state, delete_token, generation, requested_at) "
            "VALUES (?, ?, 'deleting-token', 1, 1)",
            (kb_id, state),
        )


def test_organization_confirm_fences_stale_proposal_in_its_write_transaction(tmp_path):
    db, service, actor, document_id = _organization_setup(tmp_path)
    try:
        proposal = service.preview(OrganizationInstruction(text="organize"), actor)
        selected = tuple(item.id for item in proposal.executable_items)
        _set_kb_state(db, "kb-organizer", "deleting")

        with pytest.raises(KBDeleting):
            service.confirm(proposal.id, selected, actor, None, "confirm-after-delete")

        assert db.get_document_permission_by_id(document_id)["logical_path"] == "before"
        assert db.get_organization_batch(proposal.id, "confirm-after-delete") is None
    finally:
        db.close()


def test_organization_rollback_fences_stale_batch_in_its_write_transaction(tmp_path):
    db, service, actor, document_id = _organization_setup(tmp_path)
    try:
        proposal = service.preview(OrganizationInstruction(text="organize"), actor)
        selected = tuple(item.id for item in proposal.executable_items)
        applied = service.confirm(proposal.id, selected, actor, None, "apply-before-delete")
        assert db.get_document_permission_by_id(document_id)["logical_path"] == "after"
        _set_kb_state(db, "kb-organizer", "deleting")

        with pytest.raises(KBDeleting):
            service.rollback(applied.batch_id, actor, "rollback-after-delete")

        assert db.get_document_permission_by_id(document_id)["logical_path"] == "after"
        assert db.list_organization_batch_items(applied.batch_id)[0]["status"] == "applied"
    finally:
        db.close()


def test_recycle_restore_fences_delete_intent_inside_its_transaction(tmp_path):
    db = UserDB(tmp_path / "restore.db")
    actor_id = db.create_user("restorer", "Password-123!")
    document_id = db.create_document_permission("report.txt", "kb-restore", actor_id)
    actor = {"id": actor_id, "username": "restorer", "is_admin": False}
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE document_permissions SET deleted_at=10, deleted_by=?, restore_until=? "
            "WHERE id=?",
            (actor_id, 10 + RESTORE_WINDOW_SECONDS, document_id),
        )
    _set_kb_state(db, "kb-restore", "deleting")
    try:
        with pytest.raises(KBDeleting):
            restore_from_recycle(db, document_id, actor, expected_metadata_version=1, now=20)

        document = db.get_document_permission_by_id(document_id)
        assert document["deleted_at"] == 10
        assert document["metadata_version"] == 1
    finally:
        db.close()


def test_worker_retries_busy_material_job_without_losing_or_reviving_it(tmp_path, monkeypatch):
    db = UserDB(tmp_path / "worker.db")
    owner_id = db.create_user("worker-owner", "Password-123!")
    db.create_kb_metadata("worker-kb", "Worker", owner_id=owner_id)
    document_id = db.create_document_permission("queued.txt", "worker-kb", owner_id)
    job_id = db.enqueue_material_job(document_id, version_id=1)
    worker = MaterialWorker(
        WorkerServices(db=db, versions_root=tmp_path / "versions", registry=build_default_format_registry()),
        worker_id="lifecycle-worker",
    )
    entered = threading.Event()
    release = threading.Event()

    def hold_gate():
        with kb_mutation(db, "worker-kb"):
            entered.set()
            assert release.wait(timeout=5)

    gate_thread = threading.Thread(target=hold_gate)
    gate_thread.start()
    assert entered.wait(timeout=5)
    try:
        worker.run(batch=1)
    finally:
        release.set()
        gate_thread.join(timeout=5)

    waiting = db.get_index_job(job_id)
    assert waiting["status"] == "retry"
    assert waiting["lease_owner"] is None
    assert waiting["lease_expires_at"] is None

    monkeypatch.setattr(
        "rag.materials.worker.process_index_job",
        lambda job, _services: JobResult(job["id"], "done", job["version_id"], 1, 0),
    )
    worker.run(batch=1, now=waiting["available_at"] + 1)

    assert db.get_index_job(job_id)["status"] == "done"
    assert db._conn.execute("SELECT count(*) FROM document_index_jobs WHERE id=?", (job_id,)).fetchone()[0] == 1
    db.close()


@pytest.mark.parametrize("job_type", ["material_index", "repair_vector_flags", "purge_document"])
def test_inflight_worker_keeps_delete_gate_busy_through_job_settlement(
    tmp_path, monkeypatch, job_type
):
    db = UserDB(tmp_path / "inflight.db")
    owner_id = db.create_user("inflight-owner", "Password-123!")
    db.create_kb_metadata("inflight-kb", "In flight", owner_id=owner_id)
    document_id = db.create_document_permission("inflight.txt", "inflight-kb", owner_id)
    now = time.time()
    cursor = db._conn.execute(
        "INSERT INTO document_index_jobs "
        "(document_id, version_id, content_hash, job_type, job_scope, status, available_at, created_at, updated_at) "
        "VALUES (?, 1, '', ?, 'material', 'queued', 0, ?, ?)",
        (document_id, job_type, now, now),
    )
    db._conn.commit()
    job_id = int(cursor.lastrowid)
    worker = MaterialWorker(
        WorkerServices(db=db, versions_root=tmp_path / "versions", registry=build_default_format_registry()),
        worker_id="inflight-worker",
    )
    monkeypatch.setattr(
        worker,
        "_process",
        lambda job, _now: JobResult(job["id"], "done", job["version_id"], 1, 0),
    )
    settling = threading.Event()
    release = threading.Event()
    settle = db.settle_material_job

    def blocked_settlement(*args, **kwargs):
        settling.set()
        assert release.wait(timeout=5)
        return settle(*args, **kwargs)

    monkeypatch.setattr(db, "settle_material_job", blocked_settlement)
    worker_thread = threading.Thread(target=lambda: worker.run(batch=1))
    worker_thread.start()
    assert settling.wait(timeout=5)
    try:
        with pytest.raises(KBBusy), kb_mutation(db, "inflight-kb", mode="delete"):
            pass
    finally:
        release.set()
        worker_thread.join(timeout=5)

    assert not worker_thread.is_alive()
    assert db.get_index_job(job_id)["status"] == "done"
    db.close()


def test_startup_material_migration_skips_deleting_kb_and_continues_other_kbs(tmp_path):
    db = UserDB(tmp_path / "migration.db")
    actor_id = db.create_user("migration-owner", "Password-123!")
    blocked_id = db.create_document_permission("blocked.txt", "blocked-kb", actor_id)
    blocked_version = db.create_document_version(
        blocked_id, "blocked.txt", str(tmp_path / "blocked.txt"), "blocked-hash", actor_id
    )
    db.activate_document_version(blocked_id, blocked_version, "blocked-kb", "blocked.txt")
    available_id = db.create_document_permission("available.txt", "available-kb", actor_id)
    available_version = db.create_document_version(
        available_id, "available.txt", str(tmp_path / "available.txt"), "available-hash", actor_id
    )
    db.activate_document_version(available_id, available_version, "available-kb", "available.txt")
    _set_kb_state(db, "blocked-kb", "deleting")

    try:
        report = migrate_material_metadata(db)

        assert report.migrated_documents == 1
        assert report.lifecycle_skipped == 1
        assert db._conn.execute(
            "SELECT 1 FROM material_search_fts WHERE document_id=?", (blocked_id,)
        ).fetchone() is None
        assert db._conn.execute(
            "SELECT 1 FROM material_search_fts WHERE document_id=?", (available_id,)
        ).fetchone() is not None
    finally:
        db.close()


def test_startup_material_migration_moves_gate_work_off_async_context(tmp_path):
    db = UserDB(tmp_path / "async-startup-migration.db")
    actor_id = db.create_user("async-migration-owner", "Password-123!")
    document_id = db.create_document_permission("legacy.txt", "legacy-kb", actor_id)
    version_id = db.create_document_version(
        document_id, "legacy.txt", str(tmp_path / "legacy.txt"), "legacy-hash", actor_id
    )
    db.activate_document_version(document_id, version_id, "legacy-kb", "legacy.txt")

    async def run_startup_migration():
        return migrate_material_metadata(db)

    try:
        report = asyncio.run(run_startup_migration())

        assert report.migrated_documents == 1
        assert report.lifecycle_skipped == 0
        assert db.search_material_search_fts("legacy")
    finally:
        db.close()


@pytest.mark.parametrize("job_type", ["material_index", "repair_vector_flags", "purge_document"])
def test_worker_cancels_material_jobs_after_kb_delete_intent_without_external_writes(
    tmp_path, job_type
):
    db = UserDB(tmp_path / f"{job_type}.db")
    owner_id = db.create_user("delete-owner", "Password-123!")
    db.create_kb_metadata("deleting-worker-kb", "Deleting", owner_id=owner_id)
    document_id = db.create_document_permission("pending.txt", "deleting-worker-kb", owner_id)
    now = time.time()
    cursor = db._conn.execute(
        "INSERT INTO document_index_jobs "
        "(document_id, version_id, content_hash, job_type, job_scope, status, available_at, created_at, updated_at) "
        "VALUES (?, 1, '', ?, 'material', 'queued', 0, ?, ?)",
        (document_id, job_type, now, now),
    )
    db._conn.commit()
    job_id = int(cursor.lastrowid)
    _set_kb_state(db, "deleting-worker-kb", "deleting")

    class NoWriteVectorStore:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def record(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return 0

            return record

    vector_store = NoWriteVectorStore()
    worker = MaterialWorker(
        WorkerServices(
            db=db,
            versions_root=tmp_path / "versions",
            registry=build_default_format_registry(),
            vector_store=vector_store,
        ),
        worker_id="delete-aware-worker",
    )

    worker.run(batch=1)

    job = db.get_index_job(job_id)
    assert job["status"] == "cancelled"
    assert job["error_code"] == "knowledge_base_deleting"
    assert vector_store.calls == []
    assert db.get_document_permission_by_id(document_id)["current_version_id"] is None
    db.close()


def test_wiki_retry_rereads_page_and_settles_under_its_kb_permit(tmp_path, monkeypatch):
    db = UserDB(tmp_path / "wiki-worker.db")
    db.create_kb_metadata("wiki-worker-kb", "Wiki", owner_id=0)
    now = time.time()
    cursor = db._conn.execute(
        "INSERT INTO document_index_jobs "
        "(document_id, version_id, content_hash, job_type, job_scope, status, available_at, created_at, updated_at) "
        "VALUES (31, 0, '', 'wiki_reindex', 'wiki', 'queued', 0, ?, ?)",
        (now, now),
    )
    db._conn.commit()
    job_id = int(cursor.lastrowid)

    class WikiStore:
        def __init__(self):
            self.reads = 0

        def get_page(self, page_id):
            assert page_id == 31
            self.reads += 1
            return SimpleNamespace(id=31, kb_id="wiki-worker-kb")

    class WikiIndexer:
        def __init__(self):
            self.store = WikiStore()
            self.sync_calls = 0

        def sync_page(self, page_id):
            assert page_id == 31
            self.sync_calls += 1
            return 2

    indexer = WikiIndexer()
    worker = MaterialWorker(
        WorkerServices(
            db=db,
            versions_root=tmp_path / "versions",
            registry=build_default_format_registry(),
            wiki_indexer=indexer,
        ),
        worker_id="wiki-worker",
    )
    settle = db.settle_material_job
    settle_under_gate = []

    def assert_settlement_is_inside_gate(*args, **kwargs):
        with kb_mutation(db, "wiki-worker-kb"):
            settle_under_gate.append(True)
            return settle(*args, **kwargs)

    monkeypatch.setattr(db, "settle_material_job", assert_settlement_is_inside_gate)

    worker.run(batch=1)

    assert indexer.store.reads == 2
    assert indexer.sync_calls == 1
    assert settle_under_gate == [True]
    assert db.get_index_job(job_id)["status"] == "done"
    db.close()


def test_wiki_retry_does_not_reread_or_sync_after_kb_tombstone(tmp_path):
    db = UserDB(tmp_path / "wiki-deleting.db")
    db.create_kb_metadata("wiki-deleting-kb", "Wiki", owner_id=0)
    now = time.time()
    cursor = db._conn.execute(
        "INSERT INTO document_index_jobs "
        "(document_id, version_id, content_hash, job_type, job_scope, status, available_at, created_at, updated_at) "
        "VALUES (32, 0, '', 'wiki_reindex', 'wiki', 'queued', 0, ?, ?)",
        (now, now),
    )
    db._conn.commit()
    job_id = int(cursor.lastrowid)
    _set_kb_state(db, "wiki-deleting-kb", "deleted")

    class WikiStore:
        def __init__(self):
            self.reads = 0

        def get_page(self, page_id):
            assert page_id == 32
            self.reads += 1
            return SimpleNamespace(id=32, kb_id="wiki-deleting-kb")

    class WikiIndexer:
        def __init__(self):
            self.store = WikiStore()
            self.sync_calls = 0

        def sync_page(self, page_id):
            self.sync_calls += 1
            return 1

    indexer = WikiIndexer()
    worker = MaterialWorker(
        WorkerServices(
            db=db,
            versions_root=tmp_path / "versions",
            registry=build_default_format_registry(),
            wiki_indexer=indexer,
        ),
        worker_id="wiki-worker",
    )

    worker.run(batch=1)

    assert indexer.store.reads == 1
    assert indexer.sync_calls == 0
    assert db.get_index_job(job_id)["status"] == "cancelled"
    assert db.get_index_job(job_id)["error_code"] == "knowledge_base_deleted"
    db.close()


def test_wiki_retry_lookup_failure_returns_job_to_retry(tmp_path):
    db = UserDB(tmp_path / "wiki-lookup-failure.db")
    now = time.time()
    cursor = db._conn.execute(
        "INSERT INTO document_index_jobs "
        "(document_id, version_id, content_hash, job_type, job_scope, status, available_at, created_at, updated_at) "
        "VALUES (33, 0, '', 'wiki_reindex', 'wiki', 'queued', 0, ?, ?)",
        (now, now),
    )
    db._conn.commit()
    job_id = int(cursor.lastrowid)

    class FailingWikiStore:
        def get_page(self, page_id):
            raise RuntimeError(f"lookup failed for {page_id}")

    indexer = SimpleNamespace(store=FailingWikiStore())
    worker = MaterialWorker(
        WorkerServices(
            db=db,
            versions_root=tmp_path / "versions",
            registry=build_default_format_registry(),
            wiki_indexer=indexer,
        ),
        worker_id="wiki-worker",
    )

    worker.run(batch=1)

    job = db.get_index_job(job_id)
    assert job["status"] == "retry"
    assert job["error_code"] == "processing_failed"
    assert job["lease_owner"] is None
    assert job["lease_expires_at"] is None
    db.close()


def test_purge_sweep_does_not_requeue_documents_from_deleted_kbs(tmp_path):
    db = UserDB(tmp_path / "purge-sweep.db")
    owner_id = db.create_user("purge-owner", "Password-123!")
    db.create_kb_metadata("purged-kb", "Purged", owner_id=owner_id)
    document_id = db.create_document_permission("expired.txt", "purged-kb", owner_id)
    now = time.time()
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE document_permissions SET deleted_at=?, deleted_by=?, restore_until=? WHERE id=?",
            (now - 100, owner_id, now - 1, document_id),
        )
    _set_kb_state(db, "purged-kb", "deleted")

    assert scan_due_purges(db, now=now) == []
    assert db._conn.execute(
        "SELECT count(*) FROM document_index_jobs WHERE document_id=?", (document_id,)
    ).fetchone()[0] == 0
    db.close()


def _insert_job(
    db: UserDB,
    *,
    document_id: int,
    job_scope: str,
    status: str,
    lease_owner: str | None = None,
) -> int:
    now = time.time()
    cursor = db._conn.execute(
        "INSERT INTO document_index_jobs "
        "(document_id, version_id, content_hash, job_type, job_scope, status, "
        "lease_owner, lease_expires_at, available_at, created_at, updated_at) "
        "VALUES (?, 0, '', ?, ?, ?, ?, ?, 0, ?, ?)",
        (
            document_id,
            "wiki_reindex" if job_scope == "wiki" else "material_index",
            job_scope,
            status,
            lease_owner,
            now + 60 if lease_owner else None,
            now,
            now,
        ),
    )
    db._conn.commit()
    return int(cursor.lastrowid)


def test_material_job_cancellation_is_scoped_to_active_exact_document_ids(tmp_path):
    db = UserDB(tmp_path / "users.db")
    try:
        active_material = [
            _insert_job(db, document_id=12, job_scope="material", status=status, lease_owner="worker")
            for status in ("queued", "retry", "running")
        ]
        wiki_same_id = _insert_job(
            db, document_id=12, job_scope="wiki", status="running", lease_owner="wiki-worker"
        )
        material_other_id = _insert_job(
            db, document_id=13, job_scope="material", status="retry", lease_owner="other-worker"
        )
        done_material = _insert_job(db, document_id=12, job_scope="material", status="done")

        assert db.cancel_material_jobs_for_documents([12]) == 3

        for job_id in active_material:
            job = db.get_index_job(job_id)
            assert job["status"] == "cancelled"
            assert job["lease_owner"] is None
            assert job["lease_expires_at"] is None
        assert db.get_index_job(wiki_same_id)["status"] == "running"
        assert db.get_index_job(wiki_same_id)["lease_owner"] == "wiki-worker"
        assert db.get_index_job(material_other_id)["status"] == "retry"
        assert db.get_index_job(done_material)["status"] == "done"
    finally:
        db.close()


def test_wiki_job_cancellation_is_scoped_to_active_exact_page_ids(tmp_path):
    db = UserDB(tmp_path / "users.db")
    try:
        active_wiki = [
            _insert_job(db, document_id=21, job_scope="wiki", status=status, lease_owner="worker")
            for status in ("queued", "retry", "running")
        ]
        material_same_id = _insert_job(
            db, document_id=21, job_scope="material", status="running", lease_owner="material-worker"
        )
        wiki_other_id = _insert_job(
            db, document_id=22, job_scope="wiki", status="retry", lease_owner="other-worker"
        )
        done_wiki = _insert_job(db, document_id=21, job_scope="wiki", status="done")

        assert db.cancel_wiki_reindex_jobs([21]) == 3

        for job_id in active_wiki:
            job = db.get_index_job(job_id)
            assert job["status"] == "cancelled"
            assert job["lease_owner"] is None
            assert job["lease_expires_at"] is None
        assert db.get_index_job(material_same_id)["status"] == "running"
        assert db.get_index_job(material_same_id)["lease_owner"] == "material-worker"
        assert db.get_index_job(wiki_other_id)["status"] == "retry"
        assert db.get_index_job(done_wiki)["status"] == "done"
    finally:
        db.close()
