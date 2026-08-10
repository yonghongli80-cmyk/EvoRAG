from rag.knowledge_tasks import TaskService
from rag.reliability.conflicts import ConflictCandidate, ConflictClaim, persist_conflict
from rag.user_db import UserDB


def test_repeated_refusals_create_one_owner_task(tmp_path):
    db = UserDB(str(tmp_path / "tasks.db"))
    owner_id = db.create_user("owner", "Password-123!")
    reporter_ids = [
        db.create_user(username, "Password-123!")
        for username in ("alice", "bob", "carol")
    ]
    document_id = db.create_document_permission("policy.pdf", "rag_docs", owner_id)
    service = TaskService(db)

    for reporter_id in reporter_ids:
        service.record_refusal(reporter_id, "年假 怎么 申请？", document_id)

    tasks = db.list_tasks_by_type("insufficient_evidence")

    assert len(tasks) == 1
    assert tasks[0]["report_count"] == 3
    assert tasks[0]["document_id"] == document_id
    assert tasks[0]["assigned_to"] == owner_id


def test_persisted_conflict_creates_one_evidence_conflict_task(tmp_path):
    db = UserDB(str(tmp_path / "conflicts.db"))
    owner_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("policy.pdf", "rag_docs", owner_id)
    candidate = ConflictCandidate(
        claims=(
            ConflictClaim("annual leave", "days", "5", "days", "all", None, 1, True, document_id, 11),
            ConflictClaim("annual leave", "days", "7", "days", "all", None, 2, True, document_id, 12),
        ),
        citation_ids=(1, 2),
    )

    persist_conflict(db, candidate)
    persist_conflict(db, candidate)

    tasks = db.list_tasks_by_type("evidence_conflict")
    assert len(tasks) == 1
    assert tasks[0]["report_count"] == 1
    assert tasks[0]["assigned_to"] == owner_id
