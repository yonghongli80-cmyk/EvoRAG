import json
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_streaming_endpoint_yields_error_event_when_pipeline_raises(monkeypatch):
    import rag.api as api
    from rag.routers import query as query_routes

    class FailingPipeline:
        async def query_stream(self, *args, **kwargs):
            yield 'data: {"type": "token", "content": "partial"}\n\n'
            raise RuntimeError("provider secret failure")

    user_db = MagicMock()
    # `pipeline` / `user_db` 仍通过 rag.api 的模块属性注入到 rag.context 单例。
    monkeypatch.setattr(api, "pipeline", FailingPipeline())
    monkeypatch.setattr(api, "user_db", user_db)
    request = query_routes.StreamQueryRequest(question="question", conversation_id=7)

    response = await query_routes.query_stream(request, user={"id": 1})
    events = [chunk async for chunk in response.body_iterator]

    assert len(events) == 2
    payload = json.loads(events[-1].removeprefix("data: ").strip())
    assert payload == {"type": "error", "reason": "流式生成失败，请稍后重试"}
    user_db.add_message.assert_not_called()
