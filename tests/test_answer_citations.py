"""Task 5: persistent citations and assistant message IDs."""

import json

import pytest

from rag.citations import CitationRecord, citation_response
from rag.user_db import UserDB


def parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        events.append(json.loads(line[len("data: "):]))
    return events


def citation_for(doc_name: str, version_id: int) -> dict:
    return {
        "doc_name": doc_name,
        "chunk_index": 0,
        "text_preview": "preview",
        "document_id": 1,
        "document_version_id": version_id,
        "section_title": "Section",
        "is_historical": False,
        "rank": 0,
    }


def install_fake_stream_pipeline(monkeypatch, source: dict, answer: str = "answer"):
    import rag.api as api

    class FakePipeline:
        def __init__(self, kb_id=None, user_db=None):
            pass

        async def query_stream(self, question, **kwargs):
            yield f'data: {json.dumps({"type": "token", "content": answer})}\n\n'
            yield f'data: {json.dumps({"type": "sources", "sources": [source]})}\n\n'
            yield 'data: {"type": "done"}\n\n'

    api.pipeline = FakePipeline()
    monkeypatch.setattr("rag.pipeline.RAGPipeline", FakePipeline)


@pytest.fixture
def db(tmp_path):
    return UserDB(str(tmp_path / "users.db"))


@pytest.fixture
def seeded_conversation(db):
    user_id = db.create_user("alice", "Password-123!")
    return db.create_conversation(user_id, "", "file")


def citation_record():
    return CitationRecord(
        document_id=1,
        document_version_id=4,
        chunk_index=0,
        section_title="Section",
        text_preview="preview",
        rank=0,
        is_historical=False,
    )


def test_stream_done_event_returns_persisted_assistant_message_id(
    isolated_api, make_user, monkeypatch
):
    _, headers = make_user("alice")
    user_db = isolated_api.app.state.user_db
    conversation_id = user_db.create_conversation(
        user_db.get_user_by_username("alice")["id"], "", "file"
    )
    install_fake_stream_pipeline(monkeypatch, citation_for("policy.pdf", version_id=4))

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "leave?", "conversation_id": conversation_id},
    )

    events = parse_sse(response.text)
    done = next(event for event in events if event["type"] == "done")
    assert isinstance(done["assistant_message_id"], int)
    citations = user_db.get_answer_citations(done["assistant_message_id"])
    assert citations[0]["document_version_id"] == 4


def test_citation_response_never_exposes_file_path(isolated_api, make_user):
    user, _ = make_user("alice")
    citation = {
        "id": 1,
        "document_id": 1,
        "document_version_id": 4,
        "chunk_index": 0,
        "section_title": "Section",
        "text_preview": "preview",
        "rank": 0,
        "is_historical": 0,
        "version_no": 2,
        "original_filename": "policy.pdf",
    }

    response = citation_response(isolated_api.app.state.user_db, citation, user)

    assert "file_path" not in response
    assert response["download_url"].endswith("/versions/4/download")


def test_citation_insert_failure_rolls_back_assistant_message(db, seeded_conversation, monkeypatch):
    monkeypatch.setattr(
        db,
        "_insert_answer_citations_in_tx",
        lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        db.persist_assistant_answer_with_citations(
            seeded_conversation, "answer", [citation_record()], persistence_key="req-1"
        )
    assert db.get_messages(seeded_conversation) == []


def test_answer_persistence_retry_is_idempotent(db, seeded_conversation):
    first = db.persist_assistant_answer_with_citations(
        seeded_conversation, "answer", [citation_record()], persistence_key="req-1"
    )
    second = db.persist_assistant_answer_with_citations(
        seeded_conversation, "answer", [citation_record()], persistence_key="req-1"
    )
    assert first == second
    assert len(db.get_answer_citations(first)) == 1
