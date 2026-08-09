"""Task 6: structured knowledge feedback and deterministic aggregation keys."""

from types import SimpleNamespace

import pytest

from rag.citations import CitationRecord
from rag.knowledge_feedback import (
    FeedbackValidationError,
    question_hash,
    submit_knowledge_feedback,
)
from rag.user_db import UserDB


@pytest.fixture
def db(tmp_path):
    return UserDB(str(tmp_path / "users.db"))


@pytest.fixture
def users(db):
    owner_id = db.create_user("owner", "Password-123!")
    reporter_id = db.create_user("reporter", "Password-123!")
    return SimpleNamespace(owner_id=owner_id, reporter_id=reporter_id)


def _citations():
    return [
        CitationRecord(
            document_id=1,
            document_version_id=1,
            chunk_index=0,
            section_title="A",
            text_preview="a",
            rank=0,
            is_historical=False,
        ),
        CitationRecord(
            document_id=1,
            document_version_id=1,
            chunk_index=1,
            section_title="B",
            text_preview="b",
            rank=1,
            is_historical=False,
        ),
    ]


def answer_with_two_citations(db, user_id):
    conversation_id = db.create_conversation(user_id, "", "file")
    db.add_message(conversation_id, "user", "leave policy?")
    return db.persist_assistant_answer_with_citations(
        conversation_id, "answer", _citations(), persistence_key="k"
    )


def test_outdated_feedback_requires_selected_citation_when_answer_has_multiple(db, users):
    message_id = answer_with_two_citations(db, users.reporter_id)
    reporter = db.get_user_by_id(users.reporter_id)

    with pytest.raises(FeedbackValidationError, match="select a citation"):
        submit_knowledge_feedback(
            db,
            reporter,
            assistant_message_id=message_id,
            kind="outdated",
            citation_id=None,
            comment="old policy",
            suggested_answer="",
        )


def test_outdated_feedback_with_selected_citation_succeeds(db, users):
    message_id = answer_with_two_citations(db, users.reporter_id)
    reporter = db.get_user_by_id(users.reporter_id)
    citations = db.get_answer_citations(message_id)

    result = submit_knowledge_feedback(
        db,
        reporter,
        assistant_message_id=message_id,
        kind="outdated",
        citation_id=citations[0]["id"],
        comment="old policy",
        suggested_answer="new policy",
    )

    assert result["kind"] == "outdated"
    row = db.list_user_feedback(users.reporter_id)[0][0]
    assert row["citation_id"] == citations[0]["id"]
    assert row["suggested_answer"] == "new policy"


def test_feedback_rejects_message_not_owned_by_user(db, users):
    message_id = answer_with_two_citations(db, users.reporter_id)
    owner = db.get_user_by_id(users.owner_id)

    with pytest.raises(FeedbackValidationError, match="does not belong"):
        submit_knowledge_feedback(
            db,
            owner,
            assistant_message_id=message_id,
            kind="not_useful",
            citation_id=None,
            comment="unclear",
            suggested_answer="",
        )


def test_question_hash_normalizes_case_whitespace_and_chinese_punctuation():
    assert question_hash("  年假 怎么 申请？ ") == question_hash("年假 怎么 申请?")


@pytest.fixture
def seeded_feedback(isolated_api):
    db = isolated_api.app.state.user_db
    reporter_id = db.create_user("reporter", "Password-123!")
    conversation_id = db.create_conversation(reporter_id, "", "file")
    db.add_message(conversation_id, "user", "leave policy?")
    message_id = db.persist_assistant_answer_with_citations(
        conversation_id, "answer", _citations(), persistence_key="k"
    )
    user = db.get_user_by_id(reporter_id)
    submit_knowledge_feedback(
        db, user, message_id, "not_useful", None, "unclear", ""
    )
    return SimpleNamespace(submitter_name="reporter")


def test_feedback_submitter_is_hidden_from_unrelated_user(
    isolated_api, make_user, seeded_feedback
):
    _, outsider_headers = make_user("outsider")

    response = isolated_api.get(
        "/knowledge-operations/my-feedback", headers=outsider_headers
    )

    assert response.status_code == 200
    assert seeded_feedback.submitter_name not in response.text


def test_feedback_endpoint_submits_and_lists(isolated_api, make_user):
    user, headers = make_user("alice")
    db = isolated_api.app.state.user_db
    conversation_id = db.create_conversation(user["id"], "", "file")
    db.add_message(conversation_id, "user", "leave?")
    message_id = db.persist_assistant_answer_with_citations(
        conversation_id, "answer", _citations(), persistence_key="k"
    )

    res = isolated_api.post(
        "/feedback/knowledge",
        headers=headers,
        json={
            "assistant_message_id": message_id,
            "kind": "not_useful",
            "comment": "unclear",
        },
    )

    assert res.status_code == 200
    listing = isolated_api.get("/knowledge-operations/my-feedback", headers=headers)
    assert listing.json()["total"] == 1
