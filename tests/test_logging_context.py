"""Behavioral coverage for request-scoped logging fields."""

import asyncio
import inspect
import json
import logging
import threading
from contextvars import ContextVar

import pytest

import rag.logging_config as logging_config
from rag.jev.policy import KbCapabilities
from rag.jev.shadow import ShadowCase, ShadowEvaluator
from rag.models import Chunk
from rag.pipeline import RAGPipeline


@pytest.fixture(autouse=True)
def restore_root_logging():
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    try:
        yield
    finally:
        root.handlers[:] = old_handlers
        root.setLevel(old_level)


def _logging_context(**values):
    helper = getattr(logging_config, "logging_context", None)
    assert callable(helper), "logging_config.logging_context must scope all three log fields"
    return helper(**values)


def _read_fields():
    getters = [getattr(logging_config, name, None) for name in ("get_request_id", "get_kb_id", "get_route")]
    assert all(callable(getter) for getter in getters), "all three logging context getters must exist"
    return tuple(getter() for getter in getters)


@pytest.mark.parametrize("json_format", [False, True])
def test_log_formats_include_request_kb_and_route_fields(monkeypatch, capsys, json_format):
    from config import settings

    monkeypatch.setattr(settings, "log_level", "INFO")
    monkeypatch.setattr(settings, "log_json", json_format)
    logging_config.setup_logging(json_format=json_format)

    with _logging_context(request_id="req-7", kb_id="kb-3", route="rag"):
        logging.getLogger("logging-context-test").warning("safe message")

    output = capsys.readouterr().err.strip()
    if json_format:
        record = json.loads(output)
        assert {key: record[key] for key in ("request_id", "kb_id", "route")} == {
            "request_id": "req-7", "kb_id": "kb-3", "route": "rag"
        }
        assert set(record) == {"time", "level", "logger", "request_id", "kb_id", "route", "message"}
    else:
        assert "| req-7 | kb-3 | rag |" in output
        assert "safe message" in output


def test_text_format_escapes_control_characters_in_kb_id(monkeypatch, capsys):
    from config import settings

    monkeypatch.setattr(settings, "log_level", "INFO")
    monkeypatch.setattr(settings, "log_json", False)
    logging_config.setup_logging(json_format=False)
    with _logging_context(kb_id="kb-one\nforged", route="unknown-route"):
        logging.getLogger("logging-context-test").warning("safe message")
    output = capsys.readouterr().err
    assert "kb-one\\x0aforged" in output
    assert "\nforged" not in output
    assert "| other |" in output


def test_json_format_uses_json_control_character_escaping(monkeypatch, capsys):
    from config import settings

    monkeypatch.setattr(settings, "log_level", "INFO")
    monkeypatch.setattr(settings, "log_json", True)
    logging_config.setup_logging(json_format=True)
    with _logging_context(kb_id="kb-one\nforged"):
        logging.getLogger("logging-context-test").warning("safe message")
    output = capsys.readouterr().err
    record = json.loads(output)
    assert record["kb_id"] == "kb-one\nforged"
    assert "kb-one\\nforged" in output


def test_logging_context_defaults_and_nested_scope_restore_prior_values():
    assert _read_fields() == ("-", "-", "-")
    with _logging_context(request_id="outer", kb_id="kb-outer", route="agent"):
        with _logging_context(kb_id="-", route="rag"):
            assert _read_fields() == ("outer", "-", "rag")
        assert _read_fields() == ("outer", "kb-outer", "agent")
    assert _read_fields() == ("-", "-", "-")


def test_logging_context_restores_values_after_exception():
    with _logging_context(request_id="parent", kb_id="parent-kb", route="rag"):
        with pytest.raises(ValueError, match="expected"), _logging_context(request_id="child", route="error"):
            raise ValueError("expected")
        assert _read_fields() == ("parent", "parent-kb", "rag")


def test_sync_query_scope_uses_effective_kb_and_restores_parent(monkeypatch):
    import rag.pipeline as pipeline

    owner = object.__new__(RAGPipeline)
    owner._kb_id = "owner-kb"
    monkeypatch.setattr(pipeline._pipeline_query, "query", lambda *args: _read_fields())

    with _logging_context(request_id="sync-request", kb_id="parent-kb", route="agent"):
        assert owner.query("question") == ("sync-request", "owner-kb", "-")
        assert owner.query("question", kb_id="explicit-kb") == ("sync-request", "explicit-kb", "-")
        assert _read_fields() == ("sync-request", "parent-kb", "agent")


def test_sync_query_scope_uses_prepare_default_when_owner_has_no_kb_id(monkeypatch):
    import rag.pipeline as pipeline

    owner = object.__new__(RAGPipeline)
    monkeypatch.setattr(pipeline._pipeline_query, "query", lambda *args: _read_fields())

    with _logging_context(request_id="sync-request"):
        assert owner.query("question") == ("sync-request", "rag_docs", "-")


def test_private_prepare_scopes_route_and_restores_parent_after_real_preparation(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from config import settings

    candidate = Chunk(text="policy fact", doc_name="policy.md", chunk_index=0)
    owner = object.__new__(RAGPipeline)
    owner._cache = MagicMock()
    owner._cache.get.return_value = None
    owner._kb_id = "rag_docs"
    owner._user_db = None
    owner.session_id = "session"
    owner.memory = MagicMock()
    owner.memory.build_messages.return_value = []
    owner._feedback_processor = MagicMock()
    owner._feedback_processor.get_weight.return_value = 1.0
    owner._feedback_processor.get_weights.return_value = {}
    owner.reranker = MagicMock()
    rerank_routes = []
    owner.reranker.rerank.side_effect = lambda _query, chunks: (
        rerank_routes.append(logging_config.get_route()) or chunks
    )
    owner.retriever = MagicMock()
    owner.retriever.retrieve.return_value = [candidate]
    allowed = SimpleNamespace(blocked=False, reason="")
    monkeypatch.setattr(settings, "jev_mode", "off")

    with (
        patch("rag.agent.route_question", return_value="rag"),
        patch("rag.pipeline.rewrite_query", return_value="question"),
        patch("rag.pipeline.check_injection", return_value=allowed),
        _logging_context(request_id="private-request", kb_id="parent-kb", route="agent"),
    ):
        prepared, error = owner._prepare_context("question", "session", None, top_k=1)
        assert error is None
        assert prepared["route"] == "rag"
        assert rerank_routes == ["rag"]
        assert _read_fields() == ("private-request", "parent-kb", "agent")


def test_private_prepare_restores_parent_route_after_exception(monkeypatch):
    import rag.pipeline as pipeline

    owner = object.__new__(RAGPipeline)

    def fail_during_preparation(*_args, **_kwargs):
        logging_config.set_route("error")
        raise RuntimeError("prepare failed")

    monkeypatch.setattr(pipeline._pipeline_prepare, "_prepare_context", fail_during_preparation)
    with _logging_context(request_id="private-request", kb_id="parent-kb", route="agent"):
        with pytest.raises(RuntimeError, match="prepare failed"):
            owner._prepare_context("question", "session", None)
        assert _read_fields() == ("private-request", "parent-kb", "agent")


def test_public_query_stream_remains_an_async_generator():
    assert inspect.isasyncgenfunction(RAGPipeline.query_stream)


def test_http_request_id_is_normalized_and_returned(isolated_api):
    valid = isolated_api.get("/health", headers={"X-Request-ID": "client-request-42"})
    assert valid.headers["X-Request-ID"] == "client-request-42"

    for invalid in ("", "x" * 129):
        response = isolated_api.get("/health", headers={"X-Request-ID": invalid})
        generated = response.headers["X-Request-ID"]
        assert generated != invalid
        assert len(generated) <= 128
        import uuid

        uuid.UUID(generated)


def test_http_middleware_replaces_control_request_id_and_restores_context():
    import types

    import rag.api as api

    request = types.SimpleNamespace(headers={"X-Request-ID": "bad\nvalue"})
    response = types.SimpleNamespace(headers={})
    before = logging_config.get_request_id()

    async def call_next(_request):
        assert logging_config.get_request_id() != "bad\nvalue"
        return response

    result = asyncio.run(api.request_id_middleware(request, call_next))
    assert result is response
    assert len(response.headers["X-Request-ID"]) == 36
    assert logging_config.get_request_id() == before


def test_stream_advances_are_scoped_and_consumer_context_is_restored():
    class Owner:
        _kb_id = "default-kb"

        def _memory_session_id(self, session_id, user):
            return "session"

        def _prepare_context(self, question, sid, doc_name, top_k, *, tags, user, kb_id):
            observed.append(("prepare", question, _read_fields()))
            return {"route": "fixed", "answer": question, "context": [], "sources": []}, None

        async def _stream_prepared(self, question, prepared, sid, start_time, *, model, models):
            observed.append(("stream", question, _read_fields()))
            yield f"{question}-one"
            observed.append(("stream", question, _read_fields()))
            yield f"{question}-two"

    observed = []
    owner = object.__new__(RAGPipeline)
    owner._kb_id = "default-kb"
    owner._memory_session_id = Owner._memory_session_id.__get__(owner)
    owner._prepare_context = Owner._prepare_context.__get__(owner)
    owner._stream_prepared = Owner._stream_prepared.__get__(owner)

    async def run():
        with _logging_context(request_id="req-a", kb_id="-", route="-"):
            first = owner.query_stream("first")
            assert await anext(first) == "first-one"
            assert _read_fields() == ("req-a", "-", "-")
        with _logging_context(request_id="req-b", kb_id="kb-b", route="other"):
            second = owner.query_stream("second", kb_id="kb-b")
            assert await anext(second) == "second-one"
            assert _read_fields() == ("req-b", "kb-b", "other")
            assert await anext(first) == "first-two"
            assert _read_fields() == ("req-b", "kb-b", "other")
            await first.aclose()
            await second.aclose()
        assert _read_fields() == ("-", "-", "-")

    asyncio.run(run())
    assert observed == [
        ("prepare", "first", ("req-a", "default-kb", "-")),
        ("stream", "first", ("req-a", "default-kb", "fixed")),
        ("prepare", "second", ("req-b", "kb-b", "-")),
        ("stream", "second", ("req-b", "kb-b", "fixed")),
        ("stream", "first", ("req-a", "default-kb", "fixed")),
    ]


def test_request_id_does_not_follow_the_consumer_between_stream_advances():
    observed = []

    class Owner:
        _kb_id = "default-kb"

        def _memory_session_id(self, session_id, user):
            return "session"

        def _prepare_context(self, question, sid, doc_name, top_k, *, tags, user, kb_id):
            observed.append(("prepare", question, logging_config.get_request_id()))
            return {"route": "fixed", "answer": question, "context": [], "sources": []}, None

        async def _stream_prepared(self, question, prepared, sid, start_time, *, model, models):
            observed.append(("stream", question, logging_config.get_request_id()))
            yield f"{question}-one"
            observed.append(("stream", question, logging_config.get_request_id()))
            yield f"{question}-two"

    owner = object.__new__(RAGPipeline)
    owner._kb_id = "default-kb"
    owner._memory_session_id = Owner._memory_session_id.__get__(owner)
    owner._prepare_context = Owner._prepare_context.__get__(owner)
    owner._stream_prepared = Owner._stream_prepared.__get__(owner)

    async def run():
        request_token = logging_config._request_id.set("request-a")
        first = owner.query_stream("first")
        assert await anext(first) == "first-one"
        logging_config._request_id.reset(request_token)

        request_token = logging_config._request_id.set("request-b")
        second = owner.query_stream("second")
        assert await anext(second) == "second-one"
        assert await anext(first) == "first-two"
        await first.aclose()
        await second.aclose()
        logging_config._request_id.reset(request_token)

    asyncio.run(run())
    assert observed == [
        ("prepare", "first", "request-a"),
        ("stream", "first", "request-a"),
        ("prepare", "second", "request-b"),
        ("stream", "second", "request-b"),
        ("stream", "first", "request-a"),
    ]


def test_concurrent_stream_requests_keep_context_in_async_and_prepare_thread():
    observed = []
    ready = asyncio.Barrier(2)

    class Owner:
        _kb_id = "default-kb"

        def _memory_session_id(self, session_id, user):
            return "session"

        def _prepare_context(self, question, sid, doc_name, top_k, *, tags, user, kb_id):
            observed.append(("prepare", question, _read_fields()))
            return {"route": "rag", "answer": question, "context": [], "sources": []}, None

        async def _stream_prepared(self, question, prepared, sid, start_time, *, model, models):
            await ready.wait()
            observed.append(("stream", question, _read_fields()))
            yield question

    owner = object.__new__(RAGPipeline)
    owner._kb_id = "default-kb"
    owner._memory_session_id = Owner._memory_session_id.__get__(owner)
    owner._prepare_context = Owner._prepare_context.__get__(owner)
    owner._stream_prepared = Owner._stream_prepared.__get__(owner)

    async def request(request_id, kb_id):
        with _logging_context(request_id=request_id, kb_id=kb_id, route="-"):
            stream = owner.query_stream(request_id, kb_id=kb_id)
            assert await anext(stream) == request_id
            assert _read_fields() == (request_id, kb_id, "-")
            await stream.aclose()

    async def run():
        await asyncio.gather(request("req-1", "kb-1"), request("req-2", "kb-2"))

    asyncio.run(run())
    assert sorted(observed) == sorted([
        ("prepare", "req-1", ("req-1", "kb-1", "-")),
        ("prepare", "req-2", ("req-2", "kb-2", "-")),
        ("stream", "req-1", ("req-1", "kb-1", "rag")),
        ("stream", "req-2", ("req-2", "kb-2", "rag")),
    ])


def test_stream_logging_scope_restores_context_on_cancellation_and_done():
    started = asyncio.Event()
    never_release = asyncio.Event()
    closed_fields = []

    @logging_config.request_logging_context_scope
    async def blocked_stream(_owner):
        try:
            yield "ready"
            started.set()
            await never_release.wait()
        finally:
            closed_fields.append(_read_fields())

    @logging_config.request_logging_context_scope
    async def completed_stream(_owner):
        yield "ready"

    owner = type("Owner", (), {"_kb_id": "default-kb"})()

    async def run():
        with _logging_context(request_id="cancel-request", kb_id="cancel-kb", route="agent"):
            iterator = blocked_stream(owner)
            assert await anext(iterator) == "ready"
            assert _read_fields() == ("cancel-request", "cancel-kb", "agent")
            advancing = asyncio.create_task(anext(iterator))
            await started.wait()
            advancing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await advancing
            assert _read_fields() == ("cancel-request", "cancel-kb", "agent")

            completed = completed_stream(owner)
            assert await anext(completed) == "ready"
            with pytest.raises(StopAsyncIteration):
                await anext(completed)
            assert _read_fields() == ("cancel-request", "cancel-kb", "agent")
        assert _read_fields() == ("-", "-", "-")

    asyncio.run(run())
    assert closed_fields == [("cancel-request", "default-kb", "-")]


def _shadow_case(question):
    return ShadowCase(
        question=question,
        query=question,
        legacy_route="rag",
        legacy_ranks=(),
        candidates=(Chunk(text="safe", doc_name="safe.md", chunk_index=0),),
        capabilities=KbCapabilities(),
    )


def test_shadow_worker_captures_only_logging_fields_and_restores_between_requests():
    business_context = ContextVar("shadow_business_context", default="worker-default")
    entered = {name: threading.Event() for name in ("first", "second", "third")}
    release = {name: threading.Event() for name in ("first", "second", "third")}
    worker_fields = []
    callback_fields = []

    class Jev:
        def route(self, question, capabilities):
            worker_fields.append((question, _read_fields(), business_context.get()))
            entered[question].set()
            release[question].set()
            from rag.jev.policy import RouteDecision

            return RouteDecision(route="rag", should_rewrite=False, reason="ok", preflight=None)

        def select(self, question, candidates, *, query=None, top_k=None):
            return None

    evaluator = ShadowEvaluator(
        Jev(), max_workers=1, queue_limit=2,
        on_comparison=lambda comparison: callback_fields.append(_read_fields()),
    )
    try:
        with _logging_context(request_id="req-1", kb_id="kb-1", route="rag"):
            business_context.set("business-1")
            assert evaluator.submit(_shadow_case("first"))
            assert entered["first"].wait(timeout=2)
        with _logging_context(request_id="req-2", kb_id="kb-2", route="agent"):
            business_context.set("business-2")
            release["first"].set()
            assert evaluator.stats()["submitted"] == 1
            assert evaluator.submit(_shadow_case("second"))
            assert entered["second"].wait(timeout=2)
            release["second"].set()
        assert evaluator.submit(_shadow_case("third"))
        assert entered["third"].wait(timeout=2)
    finally:
        evaluator.close(wait=True)

    assert worker_fields == [
        ("first", ("req-1", "kb-1", "rag"), "worker-default"),
        ("second", ("req-2", "kb-2", "agent"), "worker-default"),
        ("third", ("-", "-", "-"), "worker-default"),
    ]
    assert callback_fields == [("req-1", "kb-1", "rag"), ("req-2", "kb-2", "agent"), ("-", "-", "-")]


def test_shadow_worker_receives_request_id_from_each_submission():
    entered = {name: threading.Event() for name in ("first", "second")}
    release = {name: threading.Event() for name in ("first", "second")}
    worker_ids = []

    class Jev:
        def route(self, question, capabilities):
            worker_ids.append((question, logging_config.get_request_id()))
            entered[question].set()
            assert release[question].wait(timeout=2)
            from rag.jev.policy import RouteDecision

            return RouteDecision(route="rag", should_rewrite=False, reason="ok", preflight=None)

        def select(self, question, candidates, *, query=None, top_k=None):
            return None

    evaluator = ShadowEvaluator(Jev(), max_workers=1, queue_limit=2)
    try:
        token = logging_config._request_id.set("request-1")
        assert evaluator.submit(_shadow_case("first"))
        assert entered["first"].wait(timeout=2)
        logging_config._request_id.reset(token)

        token = logging_config._request_id.set("request-2")
        release["first"].set()
        assert evaluator.submit(_shadow_case("second"))
        assert entered["second"].wait(timeout=2)
        release["second"].set()
        logging_config._request_id.reset(token)
    finally:
        evaluator.close(wait=True)

    assert worker_ids == [("first", "request-1"), ("second", "request-2")]
