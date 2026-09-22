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


def test_api_starts_and_stops_material_worker(monkeypatch):
    import rag.api as api
    from config import settings

    calls = []

    class FakeWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, batch):
            calls.append(("run", batch))
            api._material_worker_stop.set()
            return []

        def shutdown(self):
            calls.append(("shutdown", None))

    monkeypatch.setattr(settings, "material_worker_enabled", True, raising=False)
    monkeypatch.setattr("rag.materials.worker.MaterialWorker", FakeWorker)
    monkeypatch.setattr(api, "_material_worker_thread", None, raising=False)
    monkeypatch.setattr(api, "_material_worker_stop", __import__("threading").Event(), raising=False)

    api._start_material_worker()
    api._material_worker_thread.join(timeout=1)
    api.close_pipeline_processors()

    assert calls == [("run", settings.material_worker_count), ("shutdown", None)]


def test_api_startup_starts_material_worker_without_upload_directory(monkeypatch, tmp_path):
    """Queued material jobs must still be consumed when legacy upload data is absent."""
    import rag.api as api

    starts = []
    monkeypatch.setattr(api, "DATA_DIR", tmp_path / "missing-upload-directory")
    monkeypatch.setattr(api, "log_security_mode", lambda: None)
    monkeypatch.setattr(api, "_run_document_version_migration", lambda: None)
    monkeypatch.setattr(api, "_run_material_metadata_migration", lambda: None)
    monkeypatch.setattr(api, "_run_spreadsheet_migration", lambda: None)
    monkeypatch.setattr(api, "_start_material_worker", lambda: starts.append(True), raising=False)

    api.auto_index_on_startup()

    assert starts == [True]


def test_material_worker_loop_survives_a_transient_run_error(monkeypatch):
    """One failed polling iteration must not stop future material indexing."""
    import rag.api as api
    from config import settings

    attempts = []

    class FakeWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, batch):
            attempts.append(batch)
            if len(attempts) == 1:
                raise RuntimeError("temporary database lock")
            api._material_worker_stop.set()
            return []

        def shutdown(self):
            pass

    monkeypatch.setattr(settings, "material_worker_enabled", True)
    monkeypatch.setattr("rag.materials.worker.MaterialWorker", FakeWorker)
    monkeypatch.setattr(api, "_material_worker_thread", None, raising=False)
    monkeypatch.setattr(api, "_material_worker_stop", __import__("threading").Event(), raising=False)

    api._start_material_worker()
    api._material_worker_thread.join(timeout=2)
    api.close_pipeline_processors()

    assert attempts == [settings.material_worker_count, settings.material_worker_count]


def test_api_shutdown_keeps_qdrant_open_until_material_worker_exits(monkeypatch):
    """A slow index operation must not access Qdrant after API shutdown closes it."""
    import rag.api as api
    import rag.vector_store as vector_store

    class StillRunningThread:
        def is_alive(self):
            return True

        def join(self, timeout):
            assert timeout == 5.0

    worker = MagicMock()
    qdrant_client = MagicMock()
    monkeypatch.setattr(api, "_material_worker", worker, raising=False)
    monkeypatch.setattr(api, "_material_worker_thread", StillRunningThread(), raising=False)
    monkeypatch.setattr(api, "_material_worker_stop", __import__("threading").Event(), raising=False)
    monkeypatch.setattr(vector_store, "_client", qdrant_client)

    api.close_pipeline_processors()

    worker.shutdown.assert_not_called()
    qdrant_client.close.assert_not_called()
    assert api._material_worker is worker


def test_pipeline_close_does_not_close_an_injected_failure_recorder():
    """Injected recorders belong to the API lifecycle, mirroring jev_service ownership."""
    from rag.pipeline import RAGPipeline

    recorder = MagicMock()
    with patch("rag.pipeline.Retriever"), patch("rag.pipeline.Reranker"), patch(
        "rag.memory.DialogueMemory"
    ), patch("rag.pipeline.ExecutionTracker"):
        pipeline = RAGPipeline(
            kb_id="test_kb",
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
            failure_recorder=recorder,
        )

    assert pipeline._owns_failure_recorder is False
    pipeline.close()
    recorder.close.assert_not_called()


def test_api_shutdown_closes_the_shared_failure_recorder(monkeypatch):
    """The API-owned recorder is released once and reset, like the other processors."""
    import rag.api as api

    recorder = MagicMock()
    monkeypatch.setattr(api, "_failure_recorder", recorder)

    api.close_pipeline_processors()

    recorder.close.assert_called_once_with()
    assert api._failure_recorder is None


def test_make_pipeline_injects_the_recorder_only_when_evolution_is_enabled(monkeypatch, tmp_path):
    """Flag off: no recorder is created or injected. Flag on: the shared singleton is injected."""
    import rag.api as api
    import rag.pipeline as pipeline_module
    from config import settings

    monkeypatch.setattr(api, "_pipelines", {}, raising=False)
    monkeypatch.setattr(api, "pipeline", None)

    monkeypatch.setattr(settings, "evolution_enabled", False)
    with patch.object(pipeline_module, "RAGPipeline") as rag_pipeline_cls:
        api._get_or_create_pipeline("rag_docs")
        assert api.get_failure_recorder() is None
        assert rag_pipeline_cls.call_args.kwargs["user_db"] is api.user_db
        assert "failure_recorder" not in rag_pipeline_cls.call_args.kwargs

    monkeypatch.setattr(settings, "evolution_enabled", True)
    monkeypatch.setattr(settings, "evolution_db_path", str(tmp_path / "evolution.db"))
    with patch.object(pipeline_module, "RAGPipeline"):
        api._pipelines.clear()
        api.pipeline = None
        recorder = api.get_failure_recorder()
        assert recorder is not None
        assert api.get_failure_recorder() is recorder
        api._get_or_create_pipeline("rag_docs")

    api.close_pipeline_processors()
    assert api._failure_recorder is None
    assert recorder.stats()["closed"] is True
