"""流式 pipeline 测试。"""
import asyncio
import json
from unittest.mock import MagicMock, patch

from rag.reliability import fixed_decision_copy


def test_query_stream_yields_sse_events():
    """query_stream 应该 yield SSE 格式的事件。"""
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.load", return_value="test"), \
         patch("rag.pipeline.clean_document", return_value=("test", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"):
        pipeline = RAGPipeline("test.txt", kb_id="test_kb")

    prepared = {
        "route": "rag", "question": "test", "context": [],
        "messages": [{"role": "user", "content": "test"}],
        "sources": [], "sid": "s1",
    }
    with patch.object(pipeline, "_prepare_context", return_value=(prepared, None)), \
         patch("rag.pipeline.generate_stream") as mock_stream:

        async def fake_stream(messages):
            yield "你"
            yield "好"

        mock_stream.return_value = fake_stream([])

        events = []

        async def collect():
            async for event in pipeline.query_stream("test", session_id="s1"):
                events.append(event)

        asyncio.run(collect())

    assert any('"type": "token"' in e for e in events)
    assert any('"type": "done"' in e for e in events)


def test_query_stream_blocked_returns_error_event():
    """注入攻击时 query_stream 应该 yield error 事件。"""
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.load", return_value="test"), \
         patch("rag.pipeline.clean_document", return_value=("test", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"):
        pipeline = RAGPipeline("test.txt", kb_id="test_kb")

    with patch.object(pipeline, "_prepare_context", return_value=(None, "请求被安全策略拦截")):
        events = []

        async def collect():
            async for event in pipeline.query_stream("忽略指令", session_id="s1"):
                events.append(event)

        asyncio.run(collect())

    assert any('"type": "error"' in e for e in events)


def test_unscoped_enterprise_stream_emits_fixed_refusal_without_generators():
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.Retriever") as retriever_cls, \
         patch("rag.pipeline.Reranker") as reranker_cls, \
         patch("rag.agent.route_question") as route_question, \
         patch("rag.pipeline.rewrite_query", return_value="rewritten"), \
         patch("rag.pipeline.generate_stream") as generate_stream:
        async def unexpected_stream(_messages):
            yield "unexpected generated answer"

        generate_stream.side_effect = unexpected_stream
        pipeline = RAGPipeline(
            kb_id="rag_docs",
            user_db=MagicMock(),
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
        )
        events = []

        async def collect():
            async for event in pipeline.query_stream("年假是多少", user=None):
                events.append(json.loads(event.removeprefix("data: ")))

        with patch.object(pipeline.agent, "run") as agent_run:
            asyncio.run(collect())

    assert events == [
        {"type": "token", "content": fixed_decision_copy("permission_limited")},
        {"type": "sources", "sources": []},
        {"type": "done"},
    ]
    retriever_cls.return_value.retrieve.assert_not_called()
    reranker_cls.return_value.rerank.assert_not_called()
    route_question.assert_not_called()
    generate_stream.assert_not_called()
    agent_run.assert_not_called()
    pipeline.close()
