"""Task 2: persistent debounced material jobs and lease claiming."""

import pytest

from rag.materials.formats import build_default_format_registry
from rag.materials.jobs import claim_material_jobs, enqueue_latest_index_job
from rag.materials.worker import JobResult, MaterialWorker, WorkerServices
from rag.user_db import UserDB


@pytest.fixture
def job_db(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    return db


def test_repeated_replacement_debounces_to_latest_version(job_db):
    first = enqueue_latest_index_job(job_db, 7, 10, "a", not_before=100)
    second = enqueue_latest_index_job(job_db, 7, 11, "b", not_before=101)
    assert job_db.get_index_job(first)["status"] == "cancelled"
    assert job_db.get_index_job(second)["version_id"] == 11


def test_expired_lease_can_be_reclaimed_once(job_db):
    job_db.enqueue_material_job(document_id=7, version_id=11)
    first = claim_material_jobs(job_db, "worker-a", now=100, limit=1, lease_seconds=30)
    assert claim_material_jobs(job_db, "worker-b", now=110, limit=1, lease_seconds=30) == []
    second = claim_material_jobs(job_db, "worker-b", now=131, limit=1, lease_seconds=30)
    assert second[0]["id"] == first[0]["id"]


def test_claim_marks_job_running_with_lease(job_db):
    job_db.enqueue_material_job(document_id=7, version_id=11)
    claimed = claim_material_jobs(job_db, "worker-a", now=100, limit=5, lease_seconds=30)
    assert len(claimed) == 1
    assert claimed[0]["status"] == "running"
    assert claimed[0]["lease_owner"] == "worker-a"
    assert claimed[0]["attempt"] == 1


def test_unavailable_job_is_not_claimed(job_db):
    # available_at is 0 by helper; bump it to force waiting
    job_db.enqueue_material_job(document_id=7, version_id=11)
    job_db._conn.execute(
        "UPDATE document_index_jobs SET available_at = 200"
    )
    job_db._conn.commit()
    assert claim_material_jobs(job_db, "worker-a", now=100, limit=5, lease_seconds=30) == []


def test_worker_marks_a_completed_job_done(job_db, tmp_path, monkeypatch):
    """A claimed job must not return to the queue after successful processing."""
    job_id = job_db.enqueue_material_job(document_id=7, version_id=11)
    worker = MaterialWorker(
        WorkerServices(db=job_db, versions_root=tmp_path, registry=build_default_format_registry()),
        worker_id="worker-a",
    )
    monkeypatch.setattr(
        "rag.materials.worker.process_index_job",
        lambda job, services: JobResult(job["id"], "done", job["version_id"], 1, 0),
    )

    worker.run(batch=1)

    assert job_db.get_index_job(job_id)["status"] == "done"


def test_worker_retries_a_processing_failure(job_db, tmp_path, monkeypatch):
    """A transient processing error must leave a claim eligible for retry."""
    job_id = job_db.enqueue_material_job(document_id=7, version_id=11)
    worker = MaterialWorker(
        WorkerServices(db=job_db, versions_root=tmp_path, registry=build_default_format_registry()),
        worker_id="worker-a",
    )

    def raise_processing_error(_job, _services):
        raise RuntimeError("embedder unavailable")

    monkeypatch.setattr("rag.materials.worker.process_index_job", raise_processing_error)

    worker.run(batch=1)

    settled = job_db.get_index_job(job_id)
    assert settled["status"] == "retry"
    assert settled["error_code"] == "processing_failed"
    assert settled["lease_owner"] is None


def test_only_the_lease_owner_can_settle_a_reclaimed_job(job_db):
    """An expired worker must not overwrite the result of a newer claim."""
    job_id = job_db.enqueue_material_job(document_id=7, version_id=11)
    claim_material_jobs(job_db, "worker-a", now=100, limit=1, lease_seconds=30)
    claim_material_jobs(job_db, "worker-b", now=131, limit=1, lease_seconds=30)

    settled = job_db.settle_material_job(job_id, "worker-a", "done")

    assert settled is False
    current = job_db.get_index_job(job_id)
    assert current["status"] == "running"
    assert current["lease_owner"] == "worker-b"


def test_shutdown_does_not_release_a_lease_reclaimed_by_another_worker(job_db, tmp_path):
    """Stopping an expired worker cannot requeue a newer worker's claim."""
    job_id = job_db.enqueue_material_job(document_id=7, version_id=11)
    worker = MaterialWorker(
        WorkerServices(db=job_db, versions_root=tmp_path, registry=build_default_format_registry()),
        worker_id="worker-a",
    )
    claim_material_jobs(job_db, "worker-a", now=100, limit=1, lease_seconds=30)
    claim_material_jobs(job_db, "worker-b", now=131, limit=1, lease_seconds=30)
    worker._in_flight.append(job_id)

    worker.shutdown()

    current = job_db.get_index_job(job_id)
    assert current["status"] == "running"
    assert current["lease_owner"] == "worker-b"


def test_renewed_lease_cannot_be_reclaimed_at_its_original_expiry(job_db):
    """A slow job remains owned while its worker renews the lease."""
    job_id = job_db.enqueue_material_job(document_id=7, version_id=11)
    claim_material_jobs(job_db, "worker-a", now=100, limit=1, lease_seconds=30)

    renewed = job_db.renew_material_job(job_id, "worker-a", now=120, lease_seconds=30)

    assert renewed is True
    assert claim_material_jobs(job_db, "worker-b", now=131, limit=1, lease_seconds=30) == []
