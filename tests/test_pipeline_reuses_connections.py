from unittest.mock import MagicMock, patch

import pytest


def _build_pipeline(feedback_processor, gap_analyzer):
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.load", return_value=""), patch(
        "rag.pipeline.clean_document", return_value=("", {})
    ), patch("rag.pipeline.chunk", return_value=[]), patch(
        "rag.pipeline.deduplicate_chunks", return_value=[]
    ), patch("rag.pipeline.embed", return_value=[]), patch(
        "rag.pipeline.clear"
    ), patch("rag.pipeline.add"), patch("rag.pipeline.Retriever") as retriever_cls, patch(
        "rag.pipeline.Reranker"
    ), patch("rag.memory.DialogueMemory"), patch(
        "rag.pipeline.ExecutionTracker"
    ), patch("rag.pipeline.RedisCache"):
        retriever_cls.return_value = MagicMock()
        return RAGPipeline(
            file_path="test.txt",
            memory_db_path=":memory:",
            feedback_processor=feedback_processor,
            gap_analyzer=gap_analyzer,
        )


def test_pipeline_reuses_injected_feedback_processor_across_queries():
    from rag.models import Chunk

    feedback_processor = MagicMock()
    feedback_processor.get_weights.return_value = {}
    pipeline = _build_pipeline(feedback_processor, MagicMock())
    chunks = [Chunk(text="safe context", doc_name="doc.txt", chunk_index=0)]
    pipeline.retriever.retrieve.return_value = chunks
    pipeline.reranker.rerank.return_value = chunks
    pipeline.memory = MagicMock()

    with patch("rag.agent.route_question", return_value="rag"), patch(
        "rag.pipeline.rewrite_query", return_value="rewritten"
    ):
        pipeline._prepare_context("first", "s1", None)
        pipeline._prepare_context("second", "s1", None)

    assert pipeline._feedback_processor is feedback_processor
    assert feedback_processor.get_weights.call_count == 2
    feedback_processor.close.assert_not_called()


def test_pipeline_reuses_injected_gap_analyzer_across_queries():
    feedback_processor = MagicMock()
    feedback_processor.get_weights.return_value = {"hash": 1.0}
    gap_analyzer = MagicMock()
    gap_analyzer.is_gap.return_value = True
    pipeline = _build_pipeline(feedback_processor, gap_analyzer)

    prepared = {
        "route": "rag",
        "question": "question",
        "context": [],
        "messages": [],
        "sources": [],
        "sid": "s1",
        "chunk_hashes": ["hash"],
        "weights": {"hash": 0.1},
    }
    pipeline.memory = MagicMock()

    with patch.object(pipeline, "_prepare_context", return_value=(prepared, None)), patch(
        "rag.pipeline.generate", return_value="文档中未提及"
    ):
        pipeline.query("first")
        pipeline.query("second")

    assert pipeline._gap_analyzer is gap_analyzer
    assert gap_analyzer.is_gap.call_count == 2
    assert gap_analyzer.record_gap.call_count == 2
    gap_analyzer.close.assert_not_called()


@pytest.mark.asyncio
async def test_streaming_pipeline_reuses_injected_gap_analyzer():
    gap_analyzer = MagicMock()
    gap_analyzer.is_gap.return_value = True
    pipeline = _build_pipeline(MagicMock(), gap_analyzer)
    pipeline.memory = MagicMock()
    pipeline.memory.should_summarize.return_value = False
    prepared = {
        "route": "rag",
        "question": "question",
        "context": [],
        "messages": [],
        "sources": [],
        "sid": "s1",
        "chunk_hashes": ["hash"],
        "weights": {"hash": 0.1},
    }

    async def tokens():
        yield "answer"

    with patch.object(pipeline, "_prepare_context", return_value=(prepared, None)), patch(
        "rag.pipeline.generate_stream", return_value=tokens()
    ):
        events = [event async for event in pipeline.query_stream("question")]

    assert events[-1] == 'data: {"type": "done"}\n\n'
    gap_analyzer.record_gap.assert_called_once_with("question", 0.1)
    gap_analyzer.close.assert_not_called()


def test_api_pipeline_factory_injects_memory_db_singletons(monkeypatch):
    import rag.api as api

    feedback_processor = MagicMock()
    gap_analyzer = MagicMock()
    monkeypatch.setattr(api, "_feedback_processor", feedback_processor)
    monkeypatch.setattr(api, "_gap_analyzer", gap_analyzer, raising=False)

    with patch("rag.pipeline.RAGPipeline") as pipeline_cls:
        api._make_pipeline(kb_id="test_kb")

    pipeline_cls.assert_called_once_with(
        kb_id="test_kb",
        user_db=api.user_db,
        feedback_processor=feedback_processor,
        gap_analyzer=gap_analyzer,
    )


@pytest.mark.asyncio
async def test_gap_analytics_endpoints_reuse_api_singleton():
    import rag.api as api

    gap_analyzer = MagicMock()
    gap_analyzer.get_summary.return_value = {"total": 0, "top_questions": []}
    gap_analyzer.get_gaps.return_value = []

    with patch.object(api, "get_gap_analyzer", return_value=gap_analyzer) as get_singleton:
        admin = {"id": 1, "is_admin": True}
        assert await api.get_gap_summary(user=admin) == {
            "total": 0,
            "top_questions": [],
        }
        assert await api.get_gaps(limit=25, user=admin) == []

    assert get_singleton.call_count == 2
    gap_analyzer.get_gaps.assert_called_once_with(limit=25)
    gap_analyzer.close.assert_not_called()


def test_api_shutdown_closes_owned_processor_singletons(monkeypatch):
    import rag.api as api

    feedback_processor = MagicMock()
    gap_analyzer = MagicMock()
    monkeypatch.setattr(api, "_feedback_processor", feedback_processor)
    monkeypatch.setattr(api, "_gap_analyzer", gap_analyzer)

    api.close_pipeline_processors()

    feedback_processor.close.assert_called_once_with()
    gap_analyzer.close.assert_called_once_with()
    assert api._feedback_processor is None
    assert api._gap_analyzer is None
