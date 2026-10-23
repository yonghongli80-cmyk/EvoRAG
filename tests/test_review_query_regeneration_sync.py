from types import SimpleNamespace

import pytest

from rag.citations import CitationRecord


class _QueryPipeline:
    def __init__(self, result=None, prepared=None):
        self.result = result
        self.prepared = prepared

    def query(self, *args, **kwargs):
        return self.result

    def _memory_session_id(self, session_id, user):
        return f"u{user['id']}:{session_id}"

    def _prepare_context(self, *args, **kwargs):
        return self.prepared, None


def _citation_source(document_id, version_id, name):
    return {
        "document_id": document_id,
        "document_version_id": version_id,
        "chunk_index": 0,
        "section_title": "Section",
        "text_preview": name,
        "rank": 0,
        "is_historical": False,
        "source_kind": "document",
        "kb_id": "rag_docs",
        "source_name": name,
        "doc_name": name,
    }


def _create_document_version(db, owner_id, name):
    document_id = db.create_document_permission(name, "rag_docs", owner_id)
    version_id = db.create_document_version(
        document_id, name, "unused", f"hash-{name}", owner_id
    )
    return document_id, version_id


def test_query_persists_sources_with_exchange(isolated_api, make_user, monkeypatch):
    import rag.routers.query as query_router

    owner, headers = make_user("query_sources_owner")
    db = isolated_api.app.state.user_db
    conversation_id = db.create_conversation(owner["id"], "chat")
    document_id, version_id = _create_document_version(db, owner["id"], "query.pdf")
    source = _citation_source(document_id, version_id, "query.pdf")
    pipeline = _QueryPipeline(
        SimpleNamespace(
            answer="answer",
            sources=[source],
            confidence=SimpleNamespace(level="high", reasons=()),
            decision=SimpleNamespace(kind="answerable"),
        )
    )
    monkeypatch.setattr(query_router, "get_pipeline", lambda: pipeline)

    response = isolated_api.post(
        "/query",
        json={"question": "question", "conversation_id": conversation_id},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    messages = db.get_messages(conversation_id, owner["id"])
    assert [message["role"] for message in messages] == ["user", "assistant"]
    citation = db.get_answer_citations(messages[-1]["id"])[0]
    assert (citation["document_id"], citation["document_version_id"]) == (
        document_id,
        version_id,
    )


def test_regenerate_replaces_answer_citations_atomically_and_returns_sources(
    isolated_api, make_user, monkeypatch
):
    import rag.routers.query as query_router

    owner, headers = make_user("regen_sources_owner")
    db = isolated_api.app.state.user_db
    conversation_id = db.create_conversation(owner["id"], "chat")
    db.add_message(conversation_id, "user", "question")
    old_document_id, old_version_id = _create_document_version(
        db, owner["id"], "old.pdf"
    )
    old_message_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "old answer",
        [CitationRecord(old_document_id, old_version_id, 0, "", "old", 0, False)],
        "old-answer",
        confidence_level="low",
        confidence_reasons=("old",),
        answerability_kind="insufficient_evidence",
    )
    old_citation_id = db.get_answer_citations(old_message_id)[0]["id"]
    db.add_knowledge_feedback(
        old_message_id,
        owner["id"],
        "outdated",
        "question",
        "question-hash",
        old_citation_id,
        "",
        "audit",
    )
    new_document_id, new_version_id = _create_document_version(
        db, owner["id"], "new.pdf"
    )
    source = _citation_source(new_document_id, new_version_id, "new.pdf")
    pipeline = _QueryPipeline(
        prepared={
            "route": "fixed",
            "answer": "new answer",
            "sources": [source],
            "decision": SimpleNamespace(kind="answerable"),
            "confidence": SimpleNamespace(level="high", reasons=("new",)),
        }
    )
    monkeypatch.setattr(query_router, "get_pipeline", lambda: pipeline)

    response = isolated_api.post(
        "/regenerate",
        json={"conversation_id": conversation_id, "message_id": old_message_id},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sources"][0]["doc_name"] == "new.pdf"
    assert body["reliability"]["confidence_level"] == "high"
    citation = db.get_answer_citations(old_message_id)[0]
    assert citation["document_id"] == new_document_id
    with db.locked_read() as conn:
        feedback = conn.execute(
            "SELECT citation_id, comment FROM feedback WHERE message_id=?",
            (old_message_id,),
        ).fetchone()
    assert feedback["citation_id"] is None
    assert feedback["comment"] == "audit"


def test_regeneration_database_failure_rolls_back_content_and_citations(
    isolated_api, make_user, monkeypatch
):
    owner, _headers = make_user("regen_rollback_owner")
    db = isolated_api.app.state.user_db
    conversation_id = db.create_conversation(owner["id"], "chat")
    db.add_message(conversation_id, "user", "question")
    document_id, version_id = _create_document_version(db, owner["id"], "stable.pdf")
    assistant_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "stable answer",
        [CitationRecord(document_id, version_id, 0, "", "stable", 0, False)],
        "stable-answer",
    )
    old_citation = db.get_answer_citations(assistant_id)[0]
    original_insert = db.chat_repo._insert_answer_citations_in_tx

    def fail_after_delete(*args, **kwargs):
        raise RuntimeError("simulated citation insert failure")

    monkeypatch.setattr(db.chat_repo, "_insert_answer_citations_in_tx", fail_after_delete)
    with pytest.raises(RuntimeError, match="simulated citation insert failure"):
        db.chat_repo.replace_assistant_answer_with_citations(
            assistant_id,
            owner["id"],
            "replacement",
            [],
            {"confidence_level": "high", "confidence_reasons": [], "answerability_kind": "answerable"},
        )
    monkeypatch.setattr(db.chat_repo, "_insert_answer_citations_in_tx", original_insert)
    assert db.get_messages(conversation_id, owner["id"])[-1]["content"] == "stable answer"
    assert db.get_answer_citations(assistant_id)[0]["id"] == old_citation["id"]
