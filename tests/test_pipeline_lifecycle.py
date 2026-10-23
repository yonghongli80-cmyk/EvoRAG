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
            api._material_worker_supervisor._stop.set()
            return []

        def shutdown(self):
            calls.append(("shutdown", None))

    monkeypatch.setattr(settings, "material_worker_enabled", True, raising=False)
    monkeypatch.setattr("rag.materials.worker.MaterialWorker", FakeWorker)
    supervisor = api._material_worker_supervisor
    monkeypatch.setattr(supervisor, "_thread", None)
    monkeypatch.setattr(supervisor, "_stop", __import__("threading").Event())

    api._start_material_worker()
    supervisor._thread.join(timeout=1)
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
            api._material_worker_supervisor._stop.set()
            return []

        def shutdown(self):
            pass

    monkeypatch.setattr(settings, "material_worker_enabled", True)
    monkeypatch.setattr("rag.materials.worker.MaterialWorker", FakeWorker)
    supervisor = api._material_worker_supervisor
    monkeypatch.setattr(supervisor, "_thread", None)
    monkeypatch.setattr(supervisor, "_stop", __import__("threading").Event())

    api._start_material_worker()
    supervisor._thread.join(timeout=2)
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
    supervisor = api._material_worker_supervisor
    monkeypatch.setattr(supervisor, "_worker", worker)
    monkeypatch.setattr(supervisor, "_thread", StillRunningThread())
    monkeypatch.setattr(supervisor, "_stop", __import__("threading").Event())
    monkeypatch.setattr(vector_store, "_client", qdrant_client)

    api.close_pipeline_processors()

    worker.shutdown.assert_not_called()
    qdrant_client.close.assert_not_called()
    assert supervisor._worker is worker


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


def test_pipeline_hands_the_rollout_controller_to_its_jev_client(monkeypatch, tmp_path):
    """灰度要在服务路径上真的生效，客户端必须拿到控制器的 ``provide()``。

    只把控制器交给 pipeline 只够发布灰度键；真正决定"用当前问题集还是候选问题集"的
    是 ``JevClient.question_set_provider``。此前没有任何地方传它，于是层 4 的候选
    虽然能灰度、能归因，却永远服务不到流量（2026-09-29 端到端发现）。
    """

    from config import settings
    from rag.pipeline import RAGPipeline

    monkeypatch.setattr(settings, "jev_mode", "active")
    controller = MagicMock()
    controller.provide = MagicMock(return_value=({}, "passages-v1+candidate-9"))

    with patch("rag.pipeline.Retriever"), patch("rag.pipeline.Reranker"), patch(
        "rag.memory.DialogueMemory"
    ), patch("rag.pipeline.ExecutionTracker"), patch("rag.pipeline.RedisCache"), patch(
        "rag.feedback_processor.FeedbackProcessor"
    ), patch("rag.gap_analyzer.GapAnalyzer"):
        pipeline = RAGPipeline(
            kb_id="test_kb", memory_db_path=":memory:", question_set_rollout=controller
        )

    try:
        assert pipeline.jev._client._question_set_provider == controller.provide
    finally:
        pipeline.close()


def test_stream_endpoint_builds_a_non_default_kb_pipeline_through_the_factory(
    isolated_api, make_user
):
    """每个请求的 pipeline 必须走 ``_make_pipeline``，否则灰度候选在服务路径上不生效。

    ``RAGPipeline(...)`` 直接构造不会拿到 ``question_set_rollout``：pipeline 只在收到
    控制器时才发布每请求的灰度键，控制器缺失时 ``provide()`` 永远回落当前问题集。所以
    "候选已灰度"在服务路径上曾经等于"永不生效"（2026-09-29 由端到端归因核对发现）。
    """

    import rag.api as api

    _, headers = make_user("kb-admin", is_admin=True)
    built: list[dict] = []

    class FakePipeline:
        def query_stream(self, *args, **kwargs):
            async def events():
                yield 'data: {"type": "done", "reliability": null}\n\n'

            return events()

    def fake_make_pipeline(**kwargs):
        built.append(kwargs)
        return FakePipeline()

    with patch.object(api, "_make_pipeline", fake_make_pipeline):
        response = isolated_api.post(
            "/query/stream",
            json={"question": "灰度路径检查", "kb_id": "test_kb", "session_id": "s1"},
            headers=headers,
        )

    assert response.status_code == 200
    assert built == [{"kb_id": "test_kb"}], "非默认库的请求也必须由注入工厂构造 pipeline"

