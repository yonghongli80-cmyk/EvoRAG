"""Task 3: explainable confidence and assistant-message persistence."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from rag.reliability.answerability import decide_answerability
from rag.reliability.confidence import assess_confidence
from rag.reliability.models import EvidenceBundle, EvidenceRef, RetrievalMetrics
from rag.user_db import UserDB, VersionConflict


def evidence(
    *,
    document_id=7,
    score=0.8,
    is_current=True,
    authority_level="unknown",
):
    return EvidenceRef(
        document_id=document_id,
        document_version_id=document_id * 10,
        score=score,
        score_source="reranker" if score is not None else "unavailable",
        is_current=is_current,
        validity_status="valid",
        acl_checked=True,
        authority_level=authority_level,
    )


def bundle(*, current=(), historical=(), conflicts=()):
    scores = [item.score for item in (*current, *historical) if item.score is not None]
    return EvidenceBundle(
        question="年假是多少",
        current_evidence=tuple(current),
        historical_evidence=tuple(historical),
        structured_results=(),
        conflicts=tuple(conflicts),
        retrieval_metrics=RetrievalMetrics(
            visible_candidates=len(current) + len(historical),
            top_score=max(scores) if scores else None,
            average_score=sum(scores) / len(scores) if scores else None,
        ),
    )


def test_historical_evidence_cannot_produce_high_confidence():
    evidence_bundle = bundle(
        historical=(evidence(score=0.99, is_current=False),)
    )

    assessment = assess_confidence(
        evidence_bundle, decide_answerability(evidence_bundle)
    )

    assert assessment.level == "low"
    assert "historical" in assessment.reasons[0]


def test_refusal_has_unavailable_confidence():
    evidence_bundle = bundle()

    assessment = assess_confidence(
        evidence_bundle, decide_answerability(evidence_bundle)
    )

    assert assessment.level == "unavailable"


def test_two_consistent_current_documents_produce_high_confidence():
    evidence_bundle = bundle(
        current=(evidence(document_id=7), evidence(document_id=8))
    )

    assessment = assess_confidence(
        evidence_bundle, decide_answerability(evidence_bundle)
    )

    assert assessment.level == "high"
    assert "multiple" in assessment.reasons[0]


def test_one_authoritative_current_document_produces_high_confidence():
    evidence_bundle = bundle(
        current=(evidence(authority_level="authoritative"),)
    )

    assessment = assess_confidence(
        evidence_bundle, decide_answerability(evidence_bundle)
    )

    assert assessment.level == "high"
    assert "authoritative" in assessment.reasons[0]


def test_one_supporting_current_document_produces_medium_confidence():
    evidence_bundle = bundle(
        current=(evidence(authority_level="supporting"),)
    )

    assessment = assess_confidence(
        evidence_bundle, decide_answerability(evidence_bundle)
    )

    assert assessment.level == "medium"
    assert "%" not in " ".join(assessment.reasons)


def test_assistant_message_persists_explainable_confidence(tmp_path):
    db = UserDB(str(tmp_path / "confidence.db"))
    user_id = db.create_user("owner", "Password-123!")
    conversation_id = db.create_conversation(user_id, "Policy")

    db.add_message(
        conversation_id,
        "assistant",
        "年假为七天。",
        confidence_level="medium",
        confidence_reasons=("single qualifying current source",),
        answerability_kind="answerable",
    )

    message = db.get_messages(conversation_id, user_id)[0]
    assert message["confidence_level"] == "medium"
    assert message["confidence_reasons"] == ["single qualifying current source"]
    assert message["answerability_kind"] == "answerable"
    assert "percentage" not in message
    db.close()


def test_existing_message_rows_receive_safe_migration_defaults(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                salt TEXT NOT NULL,
                password TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                title TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            INSERT INTO users (username, salt, password) VALUES ('legacy', '', '!');
            INSERT INTO conversations (user_id, title) VALUES (1, 'Legacy');
            INSERT INTO chat_messages (conversation_id, role, content)
            VALUES (1, 'assistant', 'old answer');
            """
        )

    db = UserDB(str(db_path))
    message = db.get_messages(1, 1)[0]
    assert message["confidence_level"] == "unavailable"
    assert message["confidence_reasons"] == []
    assert message["answerability_kind"] == "answerable"
    db.close()


def test_citation_persistence_key_cannot_hide_changed_confidence(tmp_path):
    db = UserDB(str(tmp_path / "citations.db"))
    user_id = db.create_user("owner", "Password-123!")
    conversation_id = db.create_conversation(user_id, "Policy")
    kwargs = {
        "confidence_level": "medium",
        "confidence_reasons": ("single qualifying current source",),
        "answerability_kind": "answerable",
    }

    first = db.persist_assistant_answer_with_citations(
        conversation_id, "answer", [], "request-1", **kwargs
    )
    second = db.persist_assistant_answer_with_citations(
        conversation_id, "answer", [], "request-1", **kwargs
    )
    assert second == first

    with pytest.raises(VersionConflict, match="idempotency"):
        db.persist_assistant_answer_with_citations(
            conversation_id,
            "answer",
            [],
            "request-1",
            **{**kwargs, "confidence_level": "high"},
        )
    db.close()


def test_pipeline_result_carries_refusal_assessment():
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.Retriever"), patch("rag.pipeline.Reranker"):
        pipeline = RAGPipeline(
            kb_id="rag_docs",
            user_db=MagicMock(),
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
        )

    result = pipeline.query("年假是多少", user=None)

    assert result.decision.kind == "permission_limited"
    assert result.confidence.level == "unavailable"
    pipeline.close()


def test_query_api_persists_pipeline_confidence(
    isolated_api, make_user, monkeypatch
):
    import rag.api as api

    user, headers = make_user("confidence-query")
    conversation_id = api.user_db.create_conversation(user["id"], "Policy")
    monkeypatch.setattr(
        api,
        "pipeline",
        SimpleNamespace(
            query=lambda *args, **kwargs: SimpleNamespace(
                answer="年假为七天。",
                sources=[],
                decision=SimpleNamespace(kind="answerable"),
                confidence=SimpleNamespace(
                    level="medium",
                    reasons=("single qualifying current source",),
                ),
            )
        ),
    )

    response = isolated_api.post(
        "/query",
        headers=headers,
        json={"question": "年假是多少", "conversation_id": conversation_id},
    )

    assert response.status_code == 200, response.text
    assistant = api.user_db.get_messages(conversation_id, user["id"])[-1]
    assert assistant["confidence_level"] == "medium"
    assert assistant["confidence_reasons"] == ["single qualifying current source"]
    assert assistant["answerability_kind"] == "answerable"


def test_stream_api_persists_pipeline_confidence(
    isolated_api, make_user, monkeypatch
):
    import json

    import rag.api as api

    user, headers = make_user("confidence-stream")
    conversation_id = api.user_db.create_conversation(user["id"], "Policy")

    class FakePipeline:
        async def query_stream(self, *args, **kwargs):
            yield 'data: {"type": "token", "content": "年假为七天。"}\n\n'
            yield 'data: {"type": "sources", "sources": []}\n\n'
            yield "data: " + json.dumps(
                {
                    "type": "done",
                    "reliability": {
                        "confidence_level": "high",
                        "confidence_reasons": ["authoritative current source"],
                        "answerability_kind": "answerable",
                    },
                }
            ) + "\n\n"

    monkeypatch.setattr(api, "pipeline", FakePipeline())

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "年假是多少", "conversation_id": conversation_id},
    )

    assert response.status_code == 200, response.text
    assistant = api.user_db.get_messages(conversation_id, user["id"])[-1]
    assert assistant["confidence_level"] == "high"
    assert assistant["confidence_reasons"] == ["authoritative current source"]
    assert assistant["answerability_kind"] == "answerable"
