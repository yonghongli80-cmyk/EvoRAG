import json
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_streaming_endpoint_yields_error_event_when_pipeline_raises(monkeypatch):
    import rag.api as api

    class FailingPipeline:
        async def query_stream(self, *args, **kwargs):
            yield 'data: {"type": "token", "content": "partial"}\n\n'
            raise RuntimeError("provider secret failure")

    user_db = MagicMock()
    monkeypatch.setattr(api, "pipeline", FailingPipeline())
    monkeypatch.setattr(api, "user_db", user_db)
    monkeypatch.setattr(api, "_get_current_user", lambda _token: {"id": 1})
    request = api.StreamQueryRequest(question="question", conversation_id=7)

    response = await api.query_stream(
        request, user_id="user", authorization="Bearer token"
    )
    events = [chunk async for chunk in response.body_iterator]

    assert len(events) == 2
    payload = json.loads(events[-1].removeprefix("data: ").strip())
    assert payload == {"type": "error", "reason": "流式生成失败，请稍后重试"}
    user_db.add_message.assert_not_called()
