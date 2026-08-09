"""Task 2: persistent debounced material jobs and lease claiming."""

import pytest

from rag.materials.jobs import claim_material_jobs, enqueue_latest_index_job
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
    from rag.user_db import UserDB as _U
    # available_at is 0 by helper; bump it to force waiting
    job_db.enqueue_material_job(document_id=7, version_id=11)
    job_db._conn.execute(
        "UPDATE document_index_jobs SET available_at = 200"
    )
    job_db._conn.commit()
    assert claim_material_jobs(job_db, "worker-a", now=100, limit=5, lease_seconds=30) == []
