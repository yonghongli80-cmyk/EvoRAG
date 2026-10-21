"""The stream endpoint must forward the answerability decision it already knows.

`rag/routers/query.py` reads `reliability` off the pipeline's `done` event to record refusal
tasks and to persist the message, then emits its own `done` **without** it. The frontend reads
exactly that field (`done.reliability.answerability_kind`) to explain a refusal, so a stripped
`done` means the browser can never say why an answer was withheld.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator


def _done_payloads(body: str) -> list[dict]:
    payloads = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if event.get("type") == "done":
            payloads.append(event)
    return payloads


def test_stream_done_event_forwards_the_answerability_decision(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("owner_reliability")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_hr", "HR", owner["id"], "private")

    class FakePipeline:
        def __init__(self, kb_id, user_db, **kwargs):
            pass

        async def query_stream(self, question, **kwargs) -> AsyncIterator[str]:
            yield 'data: {"type":"token","content":"现有可见资料不足，无法确认答案。"}\n\n'
            yield (
                'data: {"type":"done","reliability":'
                '{"confidence_level":"medium","confidence_reasons":["single qualifying current source"],'
                '"answerability_kind":"insufficient_evidence"}}\n\n'
            )

    monkeypatch.setattr("rag.pipeline.RAGPipeline", FakePipeline)

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "R50 电池价格", "kb_id": "kb_hr"},
    )

    assert response.status_code == 200
    done = _done_payloads(response.text)
    assert done, "the stream must end with a done event"
    assert done[-1].get("reliability", {}).get("answerability_kind") == "insufficient_evidence"


def test_stream_done_event_omits_reliability_when_there_is_none(
    isolated_api, make_user, monkeypatch
):
    """没有判定可读时不要编一个空壳出来：缺省就是缺省。"""
    owner, headers = make_user("owner_no_reliability")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_hr", "HR", owner["id"], "private")

    class FakePipeline:
        def __init__(self, kb_id, user_db, **kwargs):
            pass

        async def query_stream(self, question, **kwargs) -> AsyncIterator[str]:
            yield 'data: {"type":"token","content":"答案"}\n\n'
            yield 'data: {"type":"done"}\n\n'

    monkeypatch.setattr("rag.pipeline.RAGPipeline", FakePipeline)

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "保修范围", "kb_id": "kb_hr"},
    )

    assert response.status_code == 200
    done = _done_payloads(response.text)
    assert done
    assert "reliability" not in done[-1]
