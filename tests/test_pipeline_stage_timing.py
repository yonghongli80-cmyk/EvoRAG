import asyncio

import pytest

import rag.stage_timing as stage_timing


def test_snapshot_only_includes_completed_spans_and_seal_freezes_active_span(monkeypatch):
    ticks = iter([10.0, 10.25, 10.5, 11.0])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    timings = stage_timing.StageTimings()

    token = timings.start("rerank_ms")
    assert timings.snapshot() == {}
    assert timings.finish(token) is None
    active = timings.start("generation_ms")
    assert timings.snapshot() == {"rerank_ms": 250.0}
    assert timings.seal() == {"rerank_ms": 250.0, "generation_ms": 500.0}
    late = timings.start("generation_ms")
    assert timings.finish(late) is None
    assert timings.finish(active) is None
    assert timings.snapshot() == {"rerank_ms": 250.0, "generation_ms": 500.0}


def test_measure_call_records_real_interval_even_when_callable_raises(monkeypatch):
    ticks = iter([1.0, 1.125])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    timings = stage_timing.StageTimings()

    with stage_timing.use_stage_timings(timings), pytest.raises(ValueError, match="offline failure"):
        stage_timing.measure_call("rerank_ms", lambda: (_ for _ in ()).throw(ValueError("offline failure")))

    assert timings.snapshot() == {"rerank_ms": 125.0}
    assert stage_timing.current_stage_timings() is None


def test_measure_stream_times_each_advance_without_leaking_scope_to_consumer(monkeypatch):
    ticks = iter([2.0, 2.2, 3.0, 3.3, 4.0, 4.4])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    timings = stage_timing.StageTimings()
    seen = []

    async def source():
        assert stage_timing.current_stage_timings() is timings
        yield "a"
        assert stage_timing.current_stage_timings() is timings
        yield "b"

    async def consume():
        async for item in stage_timing.measure_stream("generation_ms", source()):
            seen.append((item, stage_timing.current_stage_timings()))

    with stage_timing.use_stage_timings(timings):
        asyncio.run(consume())

    assert seen == [("a", timings), ("b", timings)]
    # Timings are .2s + .3s; the exhaustion probe contributes .4s too.
    assert timings.snapshot()["generation_ms"] == pytest.approx(900.0)
    assert stage_timing.current_stage_timings() is None


def test_measure_stream_closes_upstream_after_consumer_close(monkeypatch):
    ticks = iter([7.0, 7.1, 7.2, 7.3])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    timings = stage_timing.StageTimings()
    closed = []

    async def source():
        try:
            yield "first"
            yield "second"
        finally:
            closed.append(True)

    async def consume_one():
        stream = stage_timing.measure_stream("generation_ms", source())
        assert await anext(stream) == "first"
        await stream.aclose()
        assert closed == [True]

    with stage_timing.use_stage_timings(timings):
        asyncio.run(consume_one())

    assert timings.snapshot() == {"generation_ms": pytest.approx(100.0)}


def _timing_pipeline(**kwargs):
    from unittest.mock import MagicMock, patch

    from rag.pipeline import RAGPipeline

    with (
        patch("rag.pipeline.load", return_value="synthetic text"),
        patch("rag.pipeline.clean_document", return_value=("synthetic text", {})),
        patch("rag.pipeline.chunk", return_value=[]),
        patch("rag.pipeline.deduplicate_chunks", return_value=[]),
        patch("rag.pipeline.embed", return_value=[]),
        patch("rag.pipeline.delete_doc"),
        patch("rag.pipeline.add"),
        patch("rag.pipeline.Retriever"),
        patch("rag.pipeline.Reranker"),
    ):
        return RAGPipeline(
            "synthetic.txt", memory_db_path=":memory:",
            feedback_processor=MagicMock(), gap_analyzer=MagicMock(), **kwargs,
        )


def test_query_measures_only_the_final_generation_call(monkeypatch):
    from unittest.mock import MagicMock, patch

    import rag.pipeline as pipeline_module
    import rag.stage_timing as stage_timing

    pipeline = _timing_pipeline()
    pipeline._prepare_context = lambda *args, **kwargs: (
        {"route": "rag", "messages": [], "context": [], "sources": [], "chunk_hashes": []},
        None,
    )
    pipeline.tracker = MagicMock()
    ticks = iter([5.0, 5.375, 6.0])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    try:
        with patch.object(pipeline_module, "generate", return_value="answer"):
            assert pipeline.query("question").answer == "answer"
        trace = pipeline.tracker.save.call_args.args[0]
        assert trace.stage_timings == {"generation_ms": 375.0}
        assert trace.decision_metadata["execution_outcome"] == "success"
    finally:
        pipeline.close()


def test_sync_generation_failure_keeps_measured_duration_and_failed_outcome(monkeypatch):
    from unittest.mock import MagicMock, patch

    import pytest

    import rag.pipeline as pipeline_module
    import rag.stage_timing as stage_timing

    pipeline = _timing_pipeline()
    pipeline._prepare_context = lambda *args, **kwargs: (
        {"route": "rag", "messages": [], "context": [], "sources": [], "chunk_hashes": []},
        None,
    )
    pipeline.tracker = MagicMock()
    ticks = iter([8.0, 8.125, 9.0])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    try:
        with patch.object(pipeline_module, "generate", side_effect=ValueError("offline")), pytest.raises(
            ValueError, match="offline"
        ):
            pipeline.query("question")
        trace = pipeline.tracker.save.call_args.args[0]
        assert trace.stage_timings == {"generation_ms": 125.0}
        assert trace.decision_metadata["execution_outcome"] == "failed"
    finally:
        pipeline.close()

@pytest.mark.parametrize("route", ["default", "balanced", "arena"])
def test_query_stream_measures_provider_wait_and_excludes_consumer_pause(monkeypatch, route):
    import json
    from unittest.mock import MagicMock, patch

    import rag.pipeline as pipeline_module
    import rag.stage_timing as stage_timing

    pipeline = _timing_pipeline()
    pipeline._prepare_context = lambda *args, **kwargs: (
        {"route": "rag", "messages": [], "sources": [], "chunk_hashes": []},
        None,
    )
    pipeline.tracker = MagicMock()
    ticks = iter([0.0, 0.1, 100.0, 100.25, 101.0, 101.5, 102.0])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))

    async def source(_messages, **_kwargs):
        if route == "default":
            yield "first"
            yield "second"
        else:
            yield {"type": "token", "model": "m1", "content": "first"}
            yield {"type": "token", "model": "m1", "content": "second"}

    provider = type("Provider", (), {"is_available": lambda self: True})()
    providers = {} if route == "default" else {"m1": provider, "m2": provider}
    router = type("Router", (), {"providers": providers})()
    router.generate_stream_balanced = source
    router.generate_stream_parallel = source

    async def collect_with_pause():
        stream = pipeline.query_stream(
            "question", model="preferred" if route == "balanced" else None,
            models=["m1", "m2"] if route == "arena" else None,
        )
        first = json.loads((await anext(stream)).removeprefix("data: "))
        assert stage_timing.current_stage_timings() is None
        second = json.loads((await anext(stream)).removeprefix("data: "))
        assert stage_timing.current_stage_timings() is None
        rest = [json.loads(event.removeprefix("data: ")) async for event in stream]
        return [first, second, *rest]

    try:
        with patch.object(pipeline_module, "generate_stream", source), patch.object(
            pipeline_module, "get_llm_pool_router", return_value=router
        ):
            events = asyncio.run(collect_with_pause())
        trace = pipeline.tracker.save.call_args.args[0]
        assert [event["content"] for event in events[:2]] == ["first", "second"]
        assert events[-1]["type"] == "done"
        assert trace.stage_timings["generation_ms"] == 850.0
        assert trace.decision_metadata["execution_outcome"] == "success"
    finally:
        pipeline.close()



def test_direct_classic_preparation_attaches_rerank_duration(monkeypatch):
    from unittest.mock import patch

    import rag.pipeline as pipeline_module
    import rag.stage_timing as stage_timing
    from config import settings
    from rag.models import Chunk

    monkeypatch.setattr(settings, "jev_mode", "off")
    pipeline = _timing_pipeline()
    chunk = Chunk(text="synthetic evidence", doc_name="doc", chunk_index=0)
    pipeline.retriever.retrieve.return_value = [chunk]
    pipeline.reranker.rerank.return_value = [chunk]
    ticks = iter([1.0, 1.4])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    try:
        with patch.object(pipeline_module._agent_mod, "route_question", return_value="rag"), patch.object(
            pipeline_module, "rewrite_query", side_effect=lambda question: question
        ):
            prepared, error = pipeline._prepare_context("question", "s1", None)
        assert error is None
        assert prepared["stage_timings"]["rerank_ms"] == pytest.approx(400.0)
    finally:
        pipeline.close()


def test_jev_fallback_preparation_keeps_nested_preparation_and_rerank_timings(monkeypatch):
    from unittest.mock import MagicMock

    import rag.stage_timing as stage_timing
    from config import settings
    from rag.jev.policy import RouteDecision
    from rag.models import Chunk

    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="rag", should_rewrite=False, reason="retrieval_confident", preflight=None
    )
    jev.select.return_value = None
    pipeline = _timing_pipeline(jev_service=jev)
    chunk = Chunk(text="synthetic evidence", doc_name="doc", chunk_index=0)
    pipeline.retriever.retrieve.return_value = [chunk]
    pipeline.reranker.rerank.return_value = [chunk]
    ticks = iter([10.0, 10.1, 10.2, 10.3, 10.5, 10.7, 11.0])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    try:
        prepared, error = pipeline._prepare_context("question", "s1", None)
        assert error is None
        assert prepared["stage_timings"]["rerank_ms"] == pytest.approx(200.0)
        assert prepared["stage_timings"]["preparation_ms"] == pytest.approx(1000.0)
    finally:
        pipeline.close()


@pytest.mark.parametrize("route", ["default", "balanced", "arena"])
def test_public_stream_close_closes_measured_provider_iterator(route):
    import json
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    import rag.pipeline as pipeline_module

    pipeline = _timing_pipeline()
    pipeline._prepare_context = lambda *args, **kwargs: (
        {"route": "rag", "messages": [], "sources": [], "chunk_hashes": []}, None
    )
    pipeline.tracker = MagicMock()
    closed = []

    async def token_source(_messages, **_kwargs):
        try:
            if route == "default":
                yield "first token"
                yield "later token"
            else:
                yield {"type": "token", "model": "m1", "content": "first token"}
                yield {"type": "token", "model": "m1", "content": "later token"}
        finally:
            closed.append(True)

    provider = SimpleNamespace(is_available=lambda: True)
    providers = {} if route == "default" else {"m1": provider, "m2": provider}
    router = SimpleNamespace(
        providers=providers,
        generate_stream_balanced=token_source,
        generate_stream_parallel=token_source,
    )

    async def open_and_close_after_first_token():
        stream = pipeline.query_stream(
            "question", models=["m1", "m2"] if route == "arena" else None
        )
        event = json.loads((await anext(stream)).removeprefix("data: "))
        assert event["type"] == "token"
        await stream.aclose()
        assert closed == [True]

    try:
        with patch.object(pipeline_module, "generate_stream", token_source), patch.object(
            pipeline_module, "get_llm_pool_router", return_value=router
        ):
            asyncio.run(open_and_close_after_first_token())
        assert closed == [True]
    finally:
        pipeline.close()

@pytest.mark.parametrize("route", ["fixed", "structured", "blocked", "agent"])
def test_completed_non_generation_routes_do_not_add_generation_samples(route):
    from unittest.mock import MagicMock

    pipeline = _timing_pipeline()
    pipeline.tracker = MagicMock()
    if route == "blocked":
        pipeline._prepare_context = lambda *args, **kwargs: (None, "blocked")
    elif route == "agent":
        pipeline._prepare_context = lambda *args, **kwargs: (
            {"route": "agent", "question": "question"}, None
        )
        pipeline.agent.run = MagicMock(return_value="tool answer")
    else:
        pipeline._prepare_context = lambda *args, **kwargs: (
            {"route": route, "answer": "direct answer", "context": [], "sources": []}, None
        )

    try:
        assert pipeline.query("question").answer in {"direct answer", "tool answer", "blocked"}
        trace = pipeline.tracker.save.call_args.args[0]
        assert "generation_ms" not in trace.stage_timings
        assert trace.decision_metadata["execution_outcome"] == "success"
    finally:
        pipeline.close()


def test_cached_query_still_does_not_create_a_trace_or_generation_sample():
    from unittest.mock import MagicMock

    pipeline = _timing_pipeline()
    pipeline.tracker = MagicMock()
    pipeline._prepare_context = lambda *args, **kwargs: (
        {"route": "cached", "answer": "cached answer", "sources": []}, None
    )
    try:
        assert pipeline.query("question").answer == "cached answer"
        pipeline.tracker.save.assert_not_called()
    finally:
        pipeline.close()

def test_stream_failure_after_token_saves_real_generation_time_without_done_or_memory(monkeypatch):
    import json
    from unittest.mock import MagicMock, patch

    import rag.pipeline as pipeline_module
    import rag.stage_timing as stage_timing

    pipeline = _timing_pipeline()
    pipeline._prepare_context = lambda *args, **kwargs: (
        {"route": "rag", "messages": [], "sources": [], "chunk_hashes": []}, None
    )
    pipeline.tracker = MagicMock()
    ticks = iter([0.0, 0.1, 50.0, 50.2, 51.0])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    closed = []

    async def broken_stream(_messages):
        try:
            yield "partial"
            raise ValueError("offline provider failure")
        finally:
            closed.append(True)

    async def collect():
        events = []
        with pytest.raises(ValueError, match="offline provider failure"):
            async for event in pipeline.query_stream("question"):
                events.append(json.loads(event.removeprefix("data: ")))
        assert closed == [True]
        return events

    try:
        with patch.object(pipeline_module, "generate_stream", broken_stream), patch.object(
            pipeline_module, "get_llm_pool_router", return_value=type("Router", (), {"providers": {}})()
        ):
            events = asyncio.run(collect())
        trace = pipeline.tracker.save.call_args.args[0]
        assert [event["type"] for event in events] == ["token"]
        assert trace.route == "error"
        assert trace.stage_timings["generation_ms"] == pytest.approx(300.0)
        assert trace.decision_metadata["execution_outcome"] == "failed"
        assert pipeline.memory.get_recent_messages(pipeline._memory_session_id(None, None)) == []
    finally:
        pipeline.close()

def test_stream_preparation_failure_without_prepared_dict_keeps_prior_spans(monkeypatch):
    from unittest.mock import MagicMock

    import rag.stage_timing as stage_timing

    pipeline = _timing_pipeline()
    pipeline.tracker = MagicMock()
    ticks = iter([1.0, 1.2, 1.5])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))

    def prepare_and_fail(*_args, **_kwargs):
        stage_timing.measure_call("rerank_ms", lambda: None)
        raise ValueError("prepare failed")

    pipeline._prepare_context = prepare_and_fail

    async def collect():
        with pytest.raises(ValueError, match="prepare failed"):
            async for _event in pipeline.query_stream("question"):
                pass

    try:
        asyncio.run(collect())
        trace = pipeline.tracker.save.call_args.args[0]
        assert trace.stage_timings["rerank_ms"] == pytest.approx(200.0)
        assert trace.decision_metadata["execution_outcome"] == "failed"
    finally:
        pipeline.close()

