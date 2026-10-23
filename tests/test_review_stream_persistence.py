"""Regression coverage for persisted turns in the SSE query endpoint."""

import json
from collections.abc import AsyncIterator

import pytest


def _events(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


@pytest.mark.parametrize(
    "answers",
    [
        ["同一个答案", "同一个答案"],
        ["第一次答案", "第二次答案"],
    ],
    ids=["same-answer", "different-answer"],
)
def test_repeated_stream_questions_persist_two_distinct_turns(
    isolated_api, make_user, monkeypatch, answers
):
    _, headers = make_user("stream_persistence")
    db = isolated_api.app.state.user_db
    user = db.get_user_by_username("stream_persistence")
    conversation_id = db.create_conversation(user["id"], "", "file")
    pending_answers = iter(answers)

    class FakePipeline:
        async def query_stream(self, question, **kwargs) -> AsyncIterator[str]:
            answer = next(pending_answers)
            yield f'data: {json.dumps({"type": "token", "content": answer})}\n\n'
            yield 'data: {"type":"done"}\n\n'

    monkeypatch.setattr("rag.routers.query.get_pipeline", lambda: FakePipeline())

    responses = [
        isolated_api.post(
            "/query/stream",
            headers=headers,
            json={"question": "重复问题", "conversation_id": conversation_id},
        )
        for _ in range(2)
    ]

    assert [response.status_code for response in responses] == [200, 200]
    turns = sorted(db.get_messages(conversation_id, user["id"]), key=lambda message: message["id"])
    assert [message["role"] for message in turns] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assistant_ids = [message["id"] for message in turns if message["role"] == "assistant"]
    assert len(set(assistant_ids)) == 2
    assert [
        next(event["assistant_message_id"] for event in _events(response.text) if event["type"] == "done")
        for response in responses
    ] == assistant_ids


def test_stream_persistence_failure_emits_error_and_rolls_back_turn(
    isolated_api, make_user, monkeypatch
):
    _, headers = make_user("stream_persistence_error")
    db = isolated_api.app.state.user_db
    user = db.get_user_by_username("stream_persistence_error")
    conversation_id = db.create_conversation(user["id"], "", "file")

    class FakePipeline:
        async def query_stream(self, question, **kwargs) -> AsyncIterator[str]:
            yield 'data: {"type":"token","content":"answer"}\n\n'
            yield 'data: {"type":"done"}\n\n'

    def fail_citation_insert(assistant_message_id, citations):
        raise RuntimeError("persistence failed")

    monkeypatch.setattr(db, "_insert_answer_citations_in_tx", fail_citation_insert)
    monkeypatch.setattr("rag.routers.query.get_pipeline", lambda: FakePipeline())

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "保存失败问题", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert events[-1]["type"] == "error"
    assert not any(event["type"] == "done" for event in events)
    assert db.get_messages(conversation_id, user["id"]) == []
