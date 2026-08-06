"""Task 11: idempotent metadata migration and bounded worker shutdown."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag.materials.formats import build_default_format_registry
from rag.materials.migration import migrate_material_metadata
from rag.materials.worker import MaterialWorker, WorkerServices
from rag.user_db import UserDB

REGISTRY = build_default_format_registry()


@pytest.fixture
def legacy_material_fixture(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    user_id = db.create_user("alice", "Password-123!")
    doc_id = db.create_document_permission("legacy.pdf", "rag_docs", user_id)
    version_id = db.create_document_version(
        doc_id, "legacy.pdf", str(tmp_path / "legacy.pdf"), "hash-legacy", user_id
    )
    db.activate_document_version(doc_id, version_id, "rag_docs", "legacy.pdf")
    fixture = SimpleNamespace(db=db, doc_id=doc_id, version_id=version_id)
    fixture.embedder = SimpleNamespace(calls=0)
    return fixture


def test_material_migration_is_idempotent_and_does_not_reembed(legacy_material_fixture):
    f = legacy_material_fixture
    first = migrate_material_metadata(f.db)
    second = migrate_material_metadata(f.db)
    assert first.migrated_documents == 1
    assert second.migrated_documents == 0
    # Migration only writes metadata/FTS; it never re-embeds.
    assert f.embedder.calls == 0
    # Logical metadata is initialized to defaults, not invented filenames.
    assert f.db.get_document_permission_by_id(f.doc_id)["logical_path"] == ""
    # The migrated document is now searchable by filename metadata.
    assert f.db.search_material_search_fts("legacy") != []


@pytest.fixture
def worker_fixture(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    user_id = db.create_user("alice", "Password-123!")
    doc_id = db.create_document_permission("contract.pdf", "rag_docs", user_id)
    version_id = db.create_document_version(
        doc_id, "contract.pdf", str(tmp_path / "v.bin"), "hash-v", user_id
    )
    job_id = db.enqueue_material_job(doc_id, version_id)
    services = WorkerServices(db=db, versions_root=tmp_path, registry=REGISTRY)
    worker = MaterialWorker(services, worker_id="worker-t11")
    return SimpleNamespace(db=db, worker=worker, job_id=job_id, version_id=version_id)


def test_shutdown_releases_leases_without_losing_jobs(worker_fixture):
    f = worker_fixture
    claimed = f.worker.start_one_job()
    assert claimed is not None
    running = f.db.get_index_job(f.job_id)
    assert running["status"] == "running"
    assert running["lease_owner"] is not None

    f.worker.shutdown(timeout=5)

    job = f.db.get_index_job(f.job_id)
    assert job["status"] in {"queued", "retry"}
    assert job["lease_owner"] is None
    # The job row survives shutdown; it can be claimed again.
    f.worker.start_one_job()
    re_claimed = f.db.get_index_job(f.job_id)
    assert re_claimed["status"] == "running"
