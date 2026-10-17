"""TDD tests for multi-model API endpoints: GET /models and POST /query/stream with multi-model parameters."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from rag.api import app


@pytest.fixture
def api_client():
    return TestClient(app)


def test_get_models_endpoint(api_client, monkeypatch):
    """GET /models must return the list of available models and the default model."""
    mock_router = MagicMock()
    mock_router.default_provider_id = "deepseek"
    mock_router.list_models.return_value = [
        {"id": "deepseek", "name": "DeepSeek V3", "model": "deepseek-chat", "healthy": True},
        {"id": "qwen", "name": "通义千问 2.5", "model": "qwen-plus", "healthy": True},
    ]

    with patch("rag.api.get_llm_pool_router", return_value=mock_router):
        response = api_client.get("/models")
        assert response.status_code == 200
        data = response.json()
        assert data["default_model"] == "deepseek"
        assert len(data["models"]) == 2
        assert data["models"][0]["id"] == "deepseek"
        assert data["models"][1]["id"] == "qwen"


def test_stream_query_request_accepts_model_and_models(api_client):
    """StreamQueryRequest model schema must accept optional model and models fields."""
    from rag.api import StreamQueryRequest

    req1 = StreamQueryRequest(question="test", model="qwen")
    assert req1.model == "qwen"
    assert req1.models is None

    req2 = StreamQueryRequest(question="test", models=["deepseek", "qwen"])
    assert req2.models == ["deepseek", "qwen"]
    assert req2.model is None


@pytest.mark.asyncio
async def test_pipeline_query_stream_parallel_arena():
    """Verify RAGPipeline.query_stream with models emits parallel tokens, sources, and a single final done."""
    from rag.pipeline import RAGPipeline

    async def fake_parallel_stream(messages, provider_ids):
        yield {"type": "token", "model": "m1", "content": "T1"}
        yield {"type": "token", "model": "m2", "content": "T2"}
        yield {"type": "model_done", "model": "m1"}
        yield {"type": "model_done", "model": "m2"}
        yield {"type": "done"}

    mock_router = MagicMock()
    mock_router.generate_stream_parallel = fake_parallel_stream

    with patch("rag.pipeline.load", return_value="doc text"), \
         patch("rag.pipeline.clean_document", return_value=("doc text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.delete_doc"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"), \
         patch("rag.llm_pool.get_llm_pool_router", return_value=mock_router):

        pipe = RAGPipeline("dummy.txt", session_id="s_arena", memory_db_path=":memory:")
        pipe._prepare_context = MagicMock(return_value=({
            "route": "rag",
            "question": "test question",
            "messages": [{"role": "user", "content": "test question"}],
            "context": [],
            "sources": [{"doc_name": "dummy.txt"}],
            "chunk_hashes": [],
            "stage_timings": {},
            "decision_metadata": {},
        }, None))

        import json
        events = []
        async for line in pipe.query_stream("test question", session_id="s_arena", models=["m1", "m2"]):
            line = line.strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        # 检查事件类型
        types = [e["type"] for e in events]
        assert "token" in types
        assert "model_done" in types
        assert "sources" in types

        # 关键验证：最后一个事件必须是 done，且倒数第二个是 sources（sources 不会被 premature done 阻断）
        assert events[-1]["type"] == "done"
        assert events[-2]["type"] == "sources"
        assert events[-2]["sources"] == [{"doc_name": "dummy.txt"}]

        # 验证只存在一个最终的 done 事件
        done_events = [e for e in events if e.get("type") == "done"]
        assert len(done_events) == 1

        pipe.close()

