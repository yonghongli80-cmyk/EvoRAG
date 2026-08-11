from unittest.mock import MagicMock, patch


def test_pipeline_close_releases_connections_once_without_closing_injected_processors():
    from rag.pipeline import RAGPipeline

    feedback_processor = MagicMock()
    gap_analyzer = MagicMock()
    with patch("rag.pipeline.Retriever"), patch("rag.pipeline.Reranker"), patch(
        "rag.memory.DialogueMemory"
    ) as memory_cls, patch(
        "rag.pipeline.ExecutionTracker"
    ) as tracker_cls, patch("rag.pipeline.RedisCache"):
        pipeline = RAGPipeline(
            kb_id="test_kb",
            memory_db_path=":memory:",
            feedback_processor=feedback_processor,
            gap_analyzer=gap_analyzer,
        )

    tracker = tracker_cls.return_value
    pipeline.close()
    pipeline.close()

    memory_cls.return_value.close.assert_called_once_with()
    tracker.close.assert_called_once_with()
    feedback_processor.close.assert_not_called()
    gap_analyzer.close.assert_not_called()


def test_pipeline_close_releases_processors_it_created():
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.Retriever"), patch("rag.pipeline.Reranker"), patch(
        "rag.memory.DialogueMemory"
    ) as memory_cls, patch(
        "rag.pipeline.ExecutionTracker"
    ) as tracker_cls, patch("rag.pipeline.RedisCache"), patch(
        "rag.feedback_processor.FeedbackProcessor"
    ) as feedback_cls, patch("rag.gap_analyzer.GapAnalyzer") as gap_cls:
        pipeline = RAGPipeline(kb_id="test_kb", memory_db_path=":memory:")

    pipeline.close()

    memory_cls.return_value.close.assert_called_once_with()
    tracker_cls.return_value.close.assert_called_once_with()
    feedback_cls.return_value.close.assert_called_once_with()
    gap_cls.return_value.close.assert_called_once_with()


def test_api_reuses_cached_pipeline_for_same_knowledge_base(monkeypatch):
    import rag.api as api

    cached = MagicMock()
    monkeypatch.setattr(api, "_pipelines", {}, raising=False)
    monkeypatch.setattr(api, "pipeline", None)

    with patch.object(api, "_make_pipeline", return_value=cached) as make_pipeline:
        first = api._get_or_create_pipeline("rag_docs")
        second = api._get_or_create_pipeline("rag_docs")

    assert first is cached
    assert second is cached
    assert api.pipeline is cached
    make_pipeline.assert_called_once_with(kb_id="rag_docs")


def test_api_shutdown_closes_cached_pipelines_before_shared_processors(monkeypatch):
    import rag.api as api

    cached = MagicMock()
    feedback_processor = MagicMock()
    gap_analyzer = MagicMock()
    monkeypatch.setattr(api, "_pipelines", {"rag_docs": cached}, raising=False)
    monkeypatch.setattr(api, "pipeline", cached)
    monkeypatch.setattr(api, "_feedback_processor", feedback_processor)
    monkeypatch.setattr(api, "_gap_analyzer", gap_analyzer)

    api.close_pipeline_processors()

    cached.close.assert_called_once_with()
    feedback_processor.close.assert_called_once_with()
    gap_analyzer.close.assert_called_once_with()
    assert api._pipelines == {}
    assert api.pipeline is None


def test_api_shutdown_closes_the_global_qdrant_client(monkeypatch):
    import rag.api as api
    import rag.vector_store as vector_store

    qdrant_client = MagicMock()
    monkeypatch.setattr(vector_store, "_client", qdrant_client)

    api.close_pipeline_processors()

    qdrant_client.close.assert_called_once_with()
    assert vector_store._client is None
