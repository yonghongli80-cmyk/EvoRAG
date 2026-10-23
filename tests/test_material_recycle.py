"""Task 7: tombstone, restore, and idempotent purge."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from rag.api_security import require_doc_view
from rag.materials.formats import build_default_format_registry
from rag.materials.recycle import (
    PURGED_STATE,
    RecycleError,
    delete_to_recycle,
    enqueue_purge_job,
    purge_document,
    restore_from_recycle,
    scan_due_purges,
)
from rag.materials.worker import MaterialWorker, WorkerServices
from rag.spreadsheets.store import resolve_dataset_path
from rag.user_db import UserDB

PDF = b"%PDF-1.4\ncontract"


class FakeVectorStore:
    def __init__(self):
        self._points = {}
        self.deleted_versions = []

    def add(self, document_id):
        self._points.setdefault(document_id, []).append("point")

    def points(self, document_id):
        return list(self._points.get(document_id, []))

    def delete_document(self, document_id):
        self._points.pop(document_id, None)

    def delete_version(self, kb_id, version_id):
        self.deleted_versions.append((kb_id, version_id))


@pytest.fixture
def recycle_fixture(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    user_id = db.create_user("alice", "Password-123!")
    doc_id = db.create_document_permission("contract.pdf", "kb-1", user_id)
    versions_root = tmp_path / "versions"
    versions_root.mkdir()

    version_file = versions_root / "v1.pdf"
    version_id = db.create_document_version(
        doc_id, "contract.pdf", str(version_file), "hash-1", user_id
    )
    db.set_version_file_path(version_id, str(version_file))
    version_file.write_bytes(PDF)
    db.activate_document_version(doc_id, version_id, "kb-1", "contract.pdf")

    db.insert_material_search_fts(doc_id, version_id, "contract.pdf", "", "", "[]", "合同正文")

    conversation_id = db.create_conversation(user_id, "c")
    message_id = db.add_message(conversation_id, "assistant", "answer")
    with db._lock, db.transaction(immediate=True):
        db._conn.execute(
            "INSERT INTO answer_citations "
            "(assistant_message_id, document_id, document_version_id, chunk_index, section_title, text_preview, rank) "
            "VALUES (?, ?, ?, 0, '', '合同正文预览', 1)",
            (message_id, doc_id, version_id),
        )

    vector_store = FakeVectorStore()
    vector_store.add(doc_id)

    datasets_root = tmp_path / "structured_datasets"
    dataset_path = resolve_dataset_path(datasets_root, doc_id, version_id)
    dataset_path.parent.mkdir(parents=True)
    dataset_path.write_bytes(b"sqlite-dataset")
    dataset_id = db.create_structured_dataset(doc_id, version_id, "xlsx", "row_groups")
    with db._lock, db.transaction(immediate=True):
        db._conn.executemany(
            "INSERT INTO material_unit_fingerprints (document_id, version_id, unit_key, digest) "
            "VALUES (?, ?, ?, ?)",
            [(doc_id, version_id, "unit-1", "digest-1"), (doc_id, version_id, "unit-2", "digest-2")],
        )

    fixture = SimpleNamespace()
    fixture.db = db
    fixture.doc_id = doc_id
    fixture.version_id = version_id
    fixture.dataset_id = dataset_id
    fixture.dataset_path = dataset_path
    fixture.datasets_root = datasets_root
    fixture.versions_root = versions_root
    fixture.vector_store = vector_store
    fixture.actor = {"id": user_id, "username": "alice", "is_admin": False}

    def open_doc(document_id):
        doc = db.get_document_permission_by_id(document_id)
        if doc is None:
            return SimpleNamespace(status_code=404)
        try:
            require_doc_view(db, doc["doc_name"], doc["kb_id"], fixture.actor)
            return SimpleNamespace(status_code=200)
        except HTTPException as exc:
            return SimpleNamespace(status_code=exc.status_code)

    fixture.open = open_doc
    fixture.download = open_doc

    def deliver_notification(document_id):
        doc = db.get_document_permission_by_id(document_id)
        if doc is not None and doc.get("deleted_at") is not None:
            return {"created": 0}
        return {"created": 1}

    fixture.deliver_notification = deliver_notification

    def search(text):
        return db.search_material_search_fts(text)

    fixture.search = search

    def current_version_id(document_id):
        return db.current_version_id(document_id)

    fixture.current_version_id = current_version_id

    def acl(document_id):
        doc = db.get_document_permission_by_id(document_id)
        return {k: doc.get(k) for k in ("owner_id", "scope", "is_public", "permission_level")}

    fixture.acl = acl

    def citation_state(document_id):
        return db.get_citation_state(document_id)

    fixture.citation_state = citation_state

    def version_file_exists(document_id):
        version = db.get_current_document_version(document_id)
        if version is None:
            return False
        return __import__("pathlib").Path(version["file_path"]).exists()

    fixture.version_file_exists = version_file_exists

    fixture.vector_points = vector_store.points
    fixture.delete = lambda document_id, actor, now: delete_to_recycle(
        db, document_id, actor, now
    )
    fixture.restore = lambda document_id, actor, expected, now: restore_from_recycle(
        db, document_id, actor, expected, now
    )
    fixture.purge = lambda document_id, now: purge_document(
        db,
        document_id,
        now,
        vector_store=vector_store,
        versions_root=versions_root,
        structured_datasets_root=datasets_root,
    )

    def make_worker(purge_scan_interval: float = 0.0) -> MaterialWorker:
        return MaterialWorker(
            WorkerServices(
                db=db,
                versions_root=versions_root,
                registry=build_default_format_registry(),
                vector_store=vector_store,
                structured_datasets_root=datasets_root,
            ),
            worker_id="worker-purge",
            purge_scan_interval=purge_scan_interval,
        )

    fixture.worker = make_worker

    def add_document(name: str) -> tuple[int, int, Path]:
        other_id = db.create_document_permission(name, "kb-1", user_id)
        other_path = versions_root / name
        other_version = db.create_document_version(
            other_id, name, str(other_path), f"hash-{name}", user_id
        )
        db.set_version_file_path(other_version, str(other_path))
        other_path.write_bytes(PDF)
        db.activate_document_version(other_id, other_version, "kb-1", name)
        return other_id, other_version, other_path

    fixture.add_document = add_document

    def snapshot(document_id):
        return SimpleNamespace(
            current_version_id=current_version_id(document_id),
            acl=acl(document_id),
        )

    fixture.snapshot = snapshot
    return fixture


def test_delete_denies_search_open_download_and_notification_immediately(recycle_fixture):
    f = recycle_fixture
    record = f.delete(f.doc_id, f.actor, now=100)
    assert record.status == "deleted"
    assert f.search("合同正文") == []
    assert f.open(f.doc_id).status_code == 404
    assert f.download(f.doc_id).status_code == 404
    assert f.deliver_notification(f.doc_id)["created"] == 0


def test_restore_before_thirty_days_preserves_version_and_acl(recycle_fixture):
    f = recycle_fixture
    before = f.snapshot(f.doc_id)
    f.delete(f.doc_id, f.actor, now=100)
    restored = f.restore(f.doc_id, f.actor, expected=2, now=100 + 29 * 86400)
    assert restored.status == "restored"
    assert f.current_version_id(f.doc_id) == before.current_version_id
    assert f.acl(f.doc_id) == before.acl
    assert f.search("合同正文") != []


def test_restore_is_denied_after_thirty_days_and_to_non_owner(recycle_fixture):
    f = recycle_fixture
    f.delete(f.doc_id, f.actor, now=100)
    with pytest.raises(RecycleError):
        f.restore(f.doc_id, f.actor, expected=2, now=100 + 31 * 86400)
    stranger = {"id": 999, "username": "mallory", "is_admin": False}
    with pytest.raises(RecycleError):
        f.restore(f.doc_id, stranger, expected=2, now=100 + 1 * 86400)


def test_purge_is_idempotent_and_keeps_minimal_citation_audit(recycle_fixture):
    f = recycle_fixture
    f.delete(f.doc_id, f.actor, now=100)
    first = f.purge(f.doc_id, now=100 + 31 * 86400)
    second = f.purge(f.doc_id, now=100 + 32 * 86400)
    assert first.removed_bytes > 0
    assert second.removed_bytes == 0
    assert f.citation_state(f.doc_id) == "purged_by_retention_policy"
    assert f.version_file_exists(f.doc_id) is False
    assert f.vector_points(f.doc_id) == []
    # The citation row survives with no text preview.
    row = f.db._conn.execute(
        "SELECT state, text_preview FROM answer_citations WHERE document_id = ?", (f.doc_id,)
    ).fetchone()
    assert row["state"] == "purged_by_retention_policy"
    assert row["text_preview"] == ""


def test_purge_requires_tombstone_deadline(recycle_fixture):
    f = recycle_fixture
    with pytest.raises(RecycleError):
        f.purge(f.doc_id, now=100)


def test_purge_releases_derived_datasets_and_fingerprints(recycle_fixture):
    """D-04: purge must release dataset rows/files and unit fingerprints, not just versions."""
    f = recycle_fixture
    f.delete(f.doc_id, f.actor, now=100)

    f.purge(f.doc_id, now=100 + 31 * 86400)

    datasets = f.db._conn.execute(
        "SELECT COUNT(*) AS total FROM structured_datasets WHERE document_id = ?", (f.doc_id,)
    ).fetchone()["total"]
    fingerprints = f.db._conn.execute(
        "SELECT COUNT(*) AS total FROM material_unit_fingerprints WHERE document_id = ?",
        (f.doc_id,),
    ).fetchone()["total"]
    assert datasets == 0
    assert fingerprints == 0
    assert f.dataset_path.exists() is False


def test_scan_enqueues_purge_only_after_the_restore_window_closes(recycle_fixture):
    """D-04: the 30-day scan drives purge through the existing job table."""
    f = recycle_fixture
    f.delete(f.doc_id, f.actor, now=100)

    assert scan_due_purges(f.db, now=100 + 29 * 86400) == []

    job_ids = scan_due_purges(f.db, now=100 + 31 * 86400)
    assert len(job_ids) == 1
    job = f.db.get_index_job(job_ids[0])
    assert job["job_type"] == "purge_document"
    assert job["document_id"] == f.doc_id
    assert job["status"] == "queued"

    # An active job is not duplicated by the next scan tick.
    assert scan_due_purges(f.db, now=100 + 31 * 86400) == []


def test_scan_skips_restored_and_already_purged_documents(recycle_fixture):
    f = recycle_fixture
    restored_id, _version, _path = f.add_document("restored.pdf")
    purged_id, _version2, _path2 = f.add_document("purged.pdf")

    f.delete(restored_id, f.actor, now=100)
    f.restore(restored_id, f.actor, expected=2, now=100 + 86400)
    f.delete(purged_id, f.actor, now=100)
    f.purge(purged_id, now=100 + 31 * 86400)
    f.delete(f.doc_id, f.actor, now=100)

    job_ids = scan_due_purges(f.db, now=100 + 32 * 86400)
    assert [f.db.get_index_job(job_id)["document_id"] for job_id in job_ids] == [f.doc_id]


def test_worker_purges_expired_documents_and_leaves_unexpired_ones(recycle_fixture):
    """D-04: the worker's periodic task purges due documents and never touches live ones."""
    f = recycle_fixture
    kept_id, kept_version, kept_path = f.add_document("draft.pdf")

    f.delete(f.doc_id, f.actor, now=100)
    f.delete(kept_id, f.actor, now=100 + 20 * 86400)

    results = f.worker().run(batch=5, now=100 + 31 * 86400)

    assert [result.status for result in results] == ["done"]
    purged = f.db.get_document_permission_by_id(f.doc_id)
    assert purged["purge_state"] == PURGED_STATE
    assert purged["current_version_id"] is None
    assert f.version_file_exists(f.doc_id) is False
    assert (f.versions_root / "v1.pdf").exists() is False
    assert f.dataset_path.exists() is False
    assert f.vector_store.deleted_versions == [("kb-1", f.version_id)]
    assert f.db._conn.execute(
        "SELECT COUNT(*) AS total FROM material_search_fts WHERE document_id = ?", (f.doc_id,)
    ).fetchone()["total"] == 0
    audit = [
        row["action"]
        for row in f.db._conn.execute(
            "SELECT action FROM material_audit_log WHERE document_id = ? ORDER BY id", (f.doc_id,)
        ).fetchall()
    ]
    assert audit == ["delete_to_recycle", "purge"]

    # The document whose window is still open keeps its file, version and dataset slot.
    kept = f.db.get_document_permission_by_id(kept_id)
    assert kept["purge_state"] == ""
    assert kept["current_version_id"] == kept_version
    assert kept_path.exists() is True


def test_worker_cancels_a_purge_for_a_still_restorable_document(recycle_fixture):
    """A stale purge job must not delete artifacts while the restore window is open."""
    f = recycle_fixture
    f.delete(f.doc_id, f.actor, now=100)
    job_id = enqueue_purge_job(f.db, f.doc_id, now=100 + 86400)

    results = f.worker().run(batch=1, now=100 + 2 * 86400)

    assert [result.status for result in results] == ["cancelled"]
    assert f.db.get_index_job(job_id)["status"] == "cancelled"
    assert f.db.get_document_permission_by_id(f.doc_id)["purge_state"] == ""
    assert (f.versions_root / "v1.pdf").exists() is True


def test_worker_retries_purge_after_vector_delete_recovers(recycle_fixture):
    """A failed vector delete must retry the persistent purge and clean old version points."""
    f = recycle_fixture
    clock_now = time.time()
    f.delete(f.doc_id, f.actor, now=clock_now - 31 * 86400)
    worker = f.worker()

    class FailOnceVectorStore:
        def __init__(self):
            self.failed = False

        def delete_version(self, kb_id, version_id):
            if not self.failed:
                self.failed = True
                raise RuntimeError("vector store temporarily unavailable")
            f.vector_store.delete_version(kb_id, version_id)

    worker.services = replace(worker.services, vector_store=FailOnceVectorStore())
    first = worker.run(batch=1, now=clock_now)
    job = f.db.get_index_job(first[0].job_id)

    assert [result.status for result in first] == ["retry"]
    assert job["status"] == "retry"
    assert f.db.get_document_permission_by_id(f.doc_id)["purge_state"] == PURGED_STATE
    assert f.db.get_document_permission_by_id(f.doc_id)["current_version_id"] is None
    assert f.vector_store.deleted_versions == []

    second = worker.run(batch=1, now=job["available_at"])

    assert [result.status for result in second] == ["done"]
    assert f.db.get_index_job(job["id"])["status"] == "done"
    assert f.vector_store.deleted_versions == [("kb-1", f.version_id)]


def test_worker_fails_purge_after_vector_delete_attempts_are_exhausted(recycle_fixture):
    """Persistent vector deletion failures exhaust the normal material job budget."""
    f = recycle_fixture
    clock_now = time.time()
    f.delete(f.doc_id, f.actor, now=clock_now - 31 * 86400)
    worker = f.worker()
    worker.max_attempts = 2

    class AlwaysFailVectorStore:
        def delete_version(self, _kb_id, _version_id):
            raise RuntimeError("vector store unavailable")

    worker.services = replace(worker.services, vector_store=AlwaysFailVectorStore())
    first = worker.run(batch=1, now=clock_now)
    retry_job = f.db.get_index_job(first[0].job_id)

    assert [result.status for result in first] == ["retry"]
    assert retry_job["status"] == "retry"

    second = worker.run(batch=1, now=retry_job["available_at"])

    assert [result.status for result in second] == ["failed"]
    assert f.db.get_index_job(retry_job["id"])["status"] == "failed"


def test_restore_cancels_a_queued_purge_job(recycle_fixture):
    f = recycle_fixture
    f.delete(f.doc_id, f.actor, now=100)
    job_id = enqueue_purge_job(f.db, f.doc_id, now=100 + 86400)

    f.restore(f.doc_id, f.actor, expected=2, now=100 + 2 * 86400)

    assert f.db.get_index_job(job_id)["status"] == "cancelled"
