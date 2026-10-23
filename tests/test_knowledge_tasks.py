"""Task 7: knowledge correction tasks, ownership, and operations API."""

from types import SimpleNamespace

import pytest

from rag.citations import CitationRecord
from rag.knowledge_feedback import submit_knowledge_feedback
from rag.knowledge_tasks import TaskService
from rag.user_db import UserDB


@pytest.fixture
def db(tmp_path):
    return UserDB(str(tmp_path / "users.db"))


@pytest.fixture
def task_fixture(db, tmp_path):
    owner_id = db.create_user("owner", "Password-123!")
    alice_id = db.create_user("alice", "Password-123!")
    bob_id = db.create_user("bob", "Password-123!")
    carol_id = db.create_user("carol", "Password-123!")
    doc_id = db.create_document_permission("policy.pdf", "rag_docs", owner_id)
    version_id = db.create_document_version(
        doc_id, "policy.pdf", str(tmp_path / "p.pdf"), "h", owner_id
    )
    db.activate_document_version(doc_id, version_id, "rag_docs", "policy.pdf")
    service = TaskService(db)
    ids = {"alice": alice_id, "bob": bob_id, "carol": carol_id}

    class Fixture:
        def submit(self, kind, user):
            user_id = ids[user]
            conv = db.create_conversation(user_id, "", "file")
            db.add_message(conv, "user", "leave?")
            citations = [
                CitationRecord(doc_id, version_id, 0, "A", "a", 0, False)
            ]
            msg = db.persist_assistant_answer_with_citations(
                conv, "answer", citations, persistence_key=f"k:{user}:{kind}"
            )
            citation_id = db.get_answer_citations(msg)[0]["id"]
            u = db.get_user_by_id(user_id)
            return submit_knowledge_feedback(
                db, u, msg, kind, citation_id, "comment", ""
            )["feedback_id"]

        @property
        def owner_id(self):
            return owner_id

        @property
        def service(self):
            return service

    return Fixture()


def test_outdated_feedback_creates_one_immediate_owner_task(task_fixture):
    first_feedback = task_fixture.submit(kind="outdated", user="alice")
    second_feedback = task_fixture.submit(kind="outdated", user="bob")

    first_task = task_fixture.service.record_feedback(first_feedback)
    second_task = task_fixture.service.record_feedback(second_feedback)

    assert second_task == first_task
    task = task_fixture.service._db.get_knowledge_task(first_task)
    assert task["assigned_to"] == task_fixture.owner_id
    assert task["report_count"] == 2


def test_not_useful_requires_three_distinct_users_within_thirty_days(task_fixture):
    ids = [
        task_fixture.submit(kind="not_useful", user="alice"),
        task_fixture.submit(kind="not_useful", user="bob"),
        task_fixture.submit(kind="not_useful", user="alice"),
    ]
    assert [task_fixture.service.record_feedback(item) for item in ids] == [None, None, None]

    third_user = task_fixture.submit(kind="not_useful", user="carol")
    task_id = task_fixture.service.record_feedback(third_user)

    assert isinstance(task_id, int)


def test_outdated_feedback_requires_selected_citation_with_multiple_sources(db):
    owner_id = db.create_user("owner", "Password-123!")
    doc_id = db.create_document_permission("policy.pdf", "rag_docs", owner_id)
    version_id = db.create_document_version(doc_id, "policy.pdf", "p.pdf", "h", owner_id)
    db.activate_document_version(doc_id, version_id, "rag_docs", "policy.pdf")
    conv = db.create_conversation(owner_id, "", "file")
    db.add_message(conv, "user", "leave?")
    citations = [
        CitationRecord(doc_id, version_id, 0, "A", "a", 0, False),
        CitationRecord(doc_id, version_id, 1, "B", "b", 1, False),
    ]
    msg = db.persist_assistant_answer_with_citations(conv, "answer", citations, "k")
    user = db.get_user_by_id(owner_id)

    from rag.knowledge_feedback import FeedbackValidationError

    with pytest.raises(FeedbackValidationError, match="select a citation"):
        submit_knowledge_feedback(
            db, user, msg, "outdated", None, "old", ""
        )


@pytest.fixture
def seeded_task(isolated_api):
    from rag.auth import create_token

    db = isolated_api.app.state.user_db
    owner_id = db.create_user("owner", "Password-123!")
    alice_id = db.create_user("alice", "Password-123!")
    doc_id = db.create_document_permission("policy.pdf", "rag_docs", owner_id)
    version_id = db.create_document_version(doc_id, "policy.pdf", "p.pdf", "h", owner_id)
    db.activate_document_version(doc_id, version_id, "rag_docs", "policy.pdf")

    conv = db.create_conversation(alice_id, "", "file")
    db.add_message(conv, "user", "leave?")
    citations = [CitationRecord(doc_id, version_id, 0, "A", "a", 0, False)]
    msg = db.persist_assistant_answer_with_citations(conv, "answer", citations, "k")
    citation_id = db.get_answer_citations(msg)[0]["id"]
    alice = db.get_user_by_id(alice_id)
    submit_knowledge_feedback(db, alice, msg, "outdated", citation_id, "old", "")

    task = db.list_tasks_for_user(owner_id)[0][0]
    token = create_token({"user_id": owner_id, "username": "owner"})
    return SimpleNamespace(
        id=task["id"],
        owner_headers={"Authorization": f"Bearer {token}"},
        reporter_names=["alice"],
        updated_at=task["updated_at"],
    )


def test_unrelated_user_cannot_read_or_transition_owner_task(
    isolated_api, make_user, seeded_task
):
    _, headers = make_user("outsider")
    listing = isolated_api.get("/knowledge-operations/my-tasks", headers=headers)
    update = isolated_api.put(
        f"/knowledge-operations/tasks/{seeded_task.id}",
        headers=headers,
        json={
            "status": "resolved",
            "note": "not mine",
            "expected_updated_at": seeded_task.updated_at,
        },
    )
    assert listing.json()["items"] == []
    assert update.status_code == 403


def test_owner_and_admin_can_see_reporter_names_but_outsider_cannot(
    isolated_api, make_user, seeded_task
):
    _, admin_headers = make_user("admin", is_admin=True)
    _, outsider_headers = make_user("outsider")

    owner_body = isolated_api.get(
        "/knowledge-operations/my-tasks", headers=seeded_task.owner_headers
    ).json()
    admin_body = isolated_api.get(
        "/knowledge-operations/tasks", headers=admin_headers
    ).json()
    outsider_body = isolated_api.get(
        "/knowledge-operations/my-tasks", headers=outsider_headers
    ).json()

    assert owner_body["items"][0]["reporters"] == seeded_task.reporter_names
    assert admin_body["items"][0]["reporters"] == seeded_task.reporter_names
    assert outsider_body["items"] == []
    assert seeded_task.reporter_names[0] not in str(outsider_body)


def test_owner_transition_resolves_task(isolated_api, seeded_task):
    response = isolated_api.put(
        f"/knowledge-operations/tasks/{seeded_task.id}",
        headers=seeded_task.owner_headers,
        json={
            "status": "resolved",
            "note": "updated",
            "expected_updated_at": seeded_task.updated_at,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"


def test_task_summary_counts_stale_knowledge_bases_by_latest_live_current_document(
    db, monkeypatch
):
    import rag.repositories.chat_repo as chat_repo
    from config import settings

    owner_id = db.create_user("summary-owner", "Password-123!")
    now = 2_000_000_000.0
    monkeypatch.setattr(chat_repo.time, "time", lambda: now)
    monkeypatch.setattr(settings, "knowledge_stale_days", 180)
    cutoff = now - 180 * 86400

    def add_document(name, kb_id, updated_at=None, *, current=True, deleted=False):
        doc_id = db.create_document_permission(name, kb_id, owner_id)
        if current:
            version_id = db.create_document_version(
                doc_id, name, f"{name}.pdf", f"hash-{name}", owner_id
            )
            db.activate_document_version(doc_id, version_id, kb_id, name)
        with db.cm.write_connection() as conn:
            conn.execute(
                "UPDATE document_permissions SET updated_at = ?, deleted_at = ? WHERE id = ?",
                (updated_at if updated_at is not None else cutoff - 10, now if deleted else None, doc_id),
            )
            conn.commit()
        return doc_id

    # Two stale files share a KB, while a separate KB is independently stale.
    add_document("old-a.pdf", "old-shared")
    add_document("old-b.pdf", "old-shared")
    add_document("old-c.pdf", "old-separate")
    # One recent current document makes the mixed KB current as a whole.
    add_document("mixed-old.pdf", "mixed", cutoff - 20)
    add_document("mixed-new.pdf", "mixed", cutoff + 20)
    # Deleted documents and rows without a current version do not qualify.
    add_document("tombstone.pdf", "tombstone", deleted=True)
    add_document("no-current.pdf", "no-current", current=False)

    summary = db.task_summary()

    assert summary["stale_knowledge_bases"] == 2
    assert set(summary) == {
        "open", "overdue", "outdated", "not_useful", "stale_knowledge_bases"
    }
