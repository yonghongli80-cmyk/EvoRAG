"""Project-construction integration tests for upstream response observers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import httpx
import httpx2
import pytest
import requests
from requests.adapters import BaseAdapter

import rag.jev.client as jev_module
import rag.reranker as reranker_module
from config import settings
from rag.jev.client import JevClient
from rag.models import Chunk
from rag.reranker import Reranker
from rag.resilience import CircuitBreaker

LOGGER_NAME = "rag.upstream_observability"


def _events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno == logging.INFO
    ]


def _observer_log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records if record.name == LOGGER_NAME)


def _clear_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "OPENAI_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)


def _expected_event(
    *,
    operation: str,
    status_code: int,
    request_id: str | None,
) -> dict:
    present = request_id is not None
    return {
        "schema_version": 1,
        "event": "upstream_http_response",
        "operation": operation,
        "status_code": status_code,
        "provider_request_id_state": "present" if present else "missing",
        "provider_request_id_header": (
            "x-request-id" if present and operation != "jev" else "x-typesafe-request-id" if present else None
        ),
        "provider_request_id_sha256": hashlib.sha256(request_id.encode("utf-8")).hexdigest() if present else None,
    }


def _same_identity_prefix(actual: list, expected: tuple) -> bool:
    return len(actual) >= len(expected) and all(
        left is right for left, right in zip(actual[: len(expected)], expected, strict=True)
    )


def _chat_completion_body(content: str) -> dict:
    return {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": "task3-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def test_agent_attaches_to_cached_default_sync_and_async_clients(monkeypatch, caplog):
    """The Agent hook must decorate LangChain's existing shared clients in both modes."""
    _clear_proxy_environment(monkeypatch)
    monkeypatch.setattr(settings, "deepseek_model", "task3-model")
    monkeypatch.setattr(settings, "deepseek_api_key", "synthetic-agent-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://task3-adapter-clients.invalid/v1")
    monkeypatch.setattr(settings, "deepseek_auth_header", "")

    import langchain.agents as langchain_agents
    import langchain_openai

    from rag.agent import RAGAgent

    real_chat_openai = langchain_openai.ChatOpenAI
    captured = []
    patched_transports = {}

    def sync_handler(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "task3-agent-sync-id"},
            json=_chat_completion_body("sync answer"),
        )

    async def async_handler(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "task3-agent-async-id"},
            json=_chat_completion_body("async answer"),
        )

    def capture_and_patch(root_client, *, mode: str, handler) -> dict:
        http_client = root_client._client
        transport = http_client._transport
        method_name = "handle_async_request" if mode == "async" else "handle_request"
        original_handler = getattr(transport, method_name)
        key = (id(transport), method_name)
        if key not in patched_transports:
            # Keep LangChain's real HTTPX client, pool, SSL and transport objects intact;
            # only replace the method body for this synthetic, in-process dispatch.
            monkeypatch.setattr(transport, method_name, handler)
            patched_transports[key] = (transport, method_name, original_handler)
        return {
            "root": root_client,
            "http_client": http_client,
            "transport": transport,
            "hooks_before": tuple(http_client.event_hooks["response"]),
            "mode": mode,
        }

    def chat_openai_factory(**kwargs):
        model = real_chat_openai(**kwargs)
        entry = {
            "model": model,
            "sync": capture_and_patch(model.root_client, mode="sync", handler=sync_handler),
            "async": capture_and_patch(model.root_async_client, mode="async", handler=async_handler),
        }
        captured.append(entry)
        return model

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", chat_openai_factory)
    monkeypatch.setattr(langchain_agents, "create_agent", lambda **kwargs: object())

    first = RAGAgent(retriever=object(), db_path=":memory:")
    second = RAGAgent(retriever=object(), db_path=":memory:")
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    try:
        first._ensure_agent()
        second._ensure_agent()

        sync_first = captured[0]["sync"]
        sync_second = captured[1]["sync"]
        async_first = captured[0]["async"]
        async_second = captured[1]["async"]

        assert captured[0]["model"] is first.llm
        assert captured[1]["model"] is second.llm
        assert first.llm.root_client is sync_first["root"]
        assert second.llm.root_client is sync_second["root"]
        assert first.llm.root_async_client is async_first["root"]
        assert second.llm.root_async_client is async_second["root"]
        assert first.llm.client is sync_first["root"].chat.completions
        assert second.llm.client is sync_second["root"].chat.completions
        assert first.llm.async_client is async_first["root"].chat.completions
        assert second.llm.async_client is async_second["root"].chat.completions

        # The two wrappers use distinct OpenAI roots over LangChain's cached HTTPX clients.
        assert sync_first["root"] is not sync_second["root"]
        assert async_first["root"] is not async_second["root"]
        assert sync_first["http_client"] is sync_second["http_client"]
        assert async_first["http_client"] is async_second["http_client"]
        assert sync_first["transport"] is sync_second["transport"]
        assert async_first["transport"] is async_second["transport"]

        for first_mode, second_mode in ((sync_first, sync_second), (async_first, async_second)):
            hooks = first_mode["http_client"].event_hooks["response"]
            assert _same_identity_prefix(hooks, first_mode["hooks_before"])
            assert len(hooks) == len(first_mode["hooks_before"]) + 1
            assert _same_identity_prefix(second_mode["hooks_before"], first_mode["hooks_before"])
            assert len(second_mode["hooks_before"]) == len(first_mode["hooks_before"]) + 1
            assert len(hooks) == len(second_mode["hooks_before"])
            assert second_mode["http_client"]._transport is second_mode["transport"]

        sync_response = second.llm.root_client.chat.completions.create(
            model="task3-model",
            messages=[{"role": "user", "content": "synthetic"}],
        )
        assert sync_response.choices[0].message.content == "sync answer"

        async def dispatch_async():
            response = await second.llm.root_async_client.chat.completions.create(
                model="task3-model",
                messages=[{"role": "user", "content": "synthetic"}],
            )
            assert response.choices[0].message.content == "async answer"
            await second.llm.root_async_client.close()
            return response

        asyncio.run(dispatch_async())
        events = _events(caplog)
        assert events == [
            _expected_event(operation="generation", status_code=200, request_id="task3-agent-sync-id"),
            _expected_event(operation="generation", status_code=200, request_id="task3-agent-async-id"),
        ]
        assert "task3-agent-sync-id" not in _observer_log_text(caplog)
        assert "task3-agent-async-id" not in _observer_log_text(caplog)
        assert second.llm.root_async_client._client.is_closed is True
    finally:
        for transport, method_name, original_handler in patched_transports.values():
            setattr(transport, method_name, original_handler)
        if captured:
            last_model = captured[-1]["model"]
            last_model.root_client.close()
            if not last_model.root_async_client._client.is_closed:
                asyncio.run(last_model.root_async_client.close())

    assert captured[0]["sync"]["http_client"].is_closed is True
    assert captured[0]["async"]["http_client"].is_closed is True


def test_jev_project_constructor_observes_httpx2_retries_and_maps_missing_id(monkeypatch, caplog):
    """The real TypeSafe client constructed by Jev logs each attempt without changing mapping."""
    monkeypatch.setattr(settings, "typesafe_api_key", "synthetic-jev-key")
    monkeypatch.setattr(settings, "jev_model", "task3-jev-model")
    monkeypatch.setattr(settings, "jev_timeout_seconds", 3.0)

    real_client_factory = jev_module.TypeSafeClient
    requests_seen = []
    clients = []
    constructor_kwargs = []
    successful_body = {
        "model": "synthetic-jev-responder",
        "usage": {"input_tokens": 6, "output_tokens": 3},
        "answers": {
            "intent": {
                "type": "choice",
                "choice": "retrieval_qa",
                "confidence": 0.94,
                "probabilities": {"retrieval_qa": 0.94},
            },
            "complexity": {
                "type": "choice",
                "choice": "simple",
                "confidence": 0.91,
                "probabilities": {"simple": 0.91},
            },
            "needs_query_rewrite": {"type": "noul", "noul": 0.08},
        },
    }

    def decode_request_body(request):
        content = request.content
        if request.headers.get("content-encoding") == "zstd":
            from backports import zstd

            content = zstd.decompress(content)
        return json.loads(content)

    def handler(request):
        requests_seen.append((request, decode_request_body(request)))
        assert request.url.path == "/v1/systemone"
        if len(requests_seen) == 1:
            return httpx2.Response(
                500,
                request=request,
                headers={"Retry-After": "0", "x-typesafe-request-id": "task3-jev-retry-id"},
                json={"error": "synthetic transient error"},
            )
        return httpx2.Response(200, request=request, json=successful_body)

    def client_factory(**kwargs):
        constructor_kwargs.append(kwargs)
        client = real_client_factory(
            **kwargs,
            base_url="https://task3-jev.invalid",
            transport=httpx2.MockTransport(handler),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(jev_module, "TypeSafeClient", client_factory)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    jev = JevClient()
    http_client = None
    try:
        result = jev.preflight({"question": "synthetic question"})

        assert result.available is True
        assert result.failure_kind is None
        assert result.value.intent == "retrieval_qa"
        assert result.value.intent_confidence == 0.94
        assert result.value.complexity == "simple"
        assert result.value.complexity_confidence == 0.91
        assert result.value.rewrite_probability == 0.08
        assert result.value.model == "synthetic-jev-responder"
        assert len(requests_seen) == 2
        first_request, body = requests_seen[0]
        assert first_request.method == "POST"
        assert set(body) == {"model", "state", "questions"}
        assert body["model"] == "task3-jev-model"
        assert body["state"] == {"question": "synthetic question"}
        assert set(body["questions"]) == {"intent", "complexity", "needs_query_rewrite"}
        assert len(clients) == 1
        assert clients[0]._config.base_url == "https://task3-jev.invalid"
        assert constructor_kwargs[0]["retry"].max_retries == 1
        assert constructor_kwargs[0]["retry"].timeout == 3.0
        assert jev._get_client() is clients[0]
        assert _events(caplog) == [
            _expected_event(operation="jev", status_code=500, request_id="task3-jev-retry-id"),
            _expected_event(operation="jev", status_code=200, request_id=None),
        ]
        assert "task3-jev-retry-id" not in _observer_log_text(caplog)
        assert "synthetic transient error" not in _observer_log_text(caplog)
    finally:
        if clients:
            http_client = clients[0]._http_client
            jev.close()

    assert http_client is not None
    assert http_client.is_closed is True
    assert jev._client is None


class _SyntheticRerankAdapter(BaseAdapter):
    def __init__(self, *, status_code: int, payload: dict, request_id: str):
        self.status_code = status_code
        self.payload = payload
        self.request_id = request_id
        self.requests = []
        self.close_calls = 0

    def send(self, request, **kwargs):
        self.requests.append(request)
        assert request.headers.get("Authorization") == "Bearer synthetic-rerank-key"
        response = requests.Response()
        response.status_code = self.status_code
        response.headers["Content-Type"] = "application/json"
        response.headers["x-request-id"] = self.request_id
        response.reason = "synthetic response"
        response._content = json.dumps(self.payload).encode("utf-8")
        response._content_consumed = True
        response.request = request
        response.url = request.url
        return response

    def close(self):
        self.close_calls += 1


def _install_reranker_session_factory(monkeypatch, *, status_code: int, payload: dict, request_id: str):
    original_session_class = requests.Session
    created = []

    def session_factory():
        session = original_session_class()
        original_hook_calls = []

        def original_hook(response, *args, **kwargs):
            original_hook_calls.append(response.status_code)
            return response

        session.hooks["response"].append(original_hook)
        adapter = _SyntheticRerankAdapter(
            status_code=status_code,
            payload=payload,
            request_id=request_id,
        )
        session.mount("https://", adapter)
        created.append(
            {
                "session": session,
                "adapter": adapter,
                "original_hook": original_hook,
                "original_hook_calls": original_hook_calls,
                "hooks_before": tuple(session.hooks["response"]),
            }
        )
        return session

    monkeypatch.setattr(reranker_module.requests, "Session", session_factory)
    return created


def _documents() -> list[Chunk]:
    return [
        Chunk(text="first synthetic document", doc_name="first.md", chunk_index=0),
        Chunk(text="second synthetic document", doc_name="second.md", chunk_index=1),
    ]


def _assert_reranker_session_closed(reranker: Reranker, created: list[dict]) -> None:
    assert len(created) == 1
    client = created[0]["session"]
    adapter = created[0]["adapter"]
    assert reranker._get_client() is client
    assert client.headers["Authorization"] == "Bearer synthetic-rerank-key"
    assert _same_identity_prefix(client.hooks["response"], created[0]["hooks_before"])
    assert len(client.hooks["response"]) == len(created[0]["hooks_before"]) + 1
    assert created[0]["original_hook_calls"] == [adapter.status_code] * len(adapter.requests)
    reranker.close()
    assert reranker._client is None
    assert adapter.close_calls == 1
    reranker.close()
    assert adapter.close_calls == 1


def test_reranker_project_session_observes_success_and_preserves_output_auth_close(monkeypatch, caplog):
    monkeypatch.setattr(reranker_module, "_breaker", CircuitBreaker(failure_threshold=5, recovery_timeout=30.0))
    docs = _documents()
    created = _install_reranker_session_factory(
        monkeypatch,
        status_code=200,
        payload={
            "results": [
                {"index": 1, "relevance_score": 0.92},
                {"index": 0, "relevance_score": 0.81},
            ]
        },
        request_id="task3-rerank-success-id",
    )
    reranker = Reranker(
        provider="siliconflow",
        model="synthetic-reranker-model",
        base_url="https://task3-reranker.invalid/v1/rerank",
        api_key="synthetic-rerank-key",
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    try:
        result = reranker.rerank("synthetic query", docs, top_k=2)

        assert [chunk.text for chunk in result] == ["second synthetic document", "first synthetic document"]
        assert [chunk.rerank_score for chunk in result] == [0.92, 0.81]
        assert len(created) == 1
        assert len(created[0]["adapter"].requests) == 1
        assert _events(caplog) == [
            _expected_event(operation="rerank", status_code=200, request_id="task3-rerank-success-id")
        ]
        assert "task3-rerank-success-id" not in _observer_log_text(caplog)
    finally:
        _assert_reranker_session_closed(reranker, created)


def test_reranker_project_session_observes_each_error_retry_and_preserves_fallback(monkeypatch, caplog):
    import rag.resilience as resilience_module

    monkeypatch.setattr(reranker_module, "_breaker", CircuitBreaker(failure_threshold=5, recovery_timeout=30.0))
    monkeypatch.setattr(resilience_module.time, "sleep", lambda _: None)
    docs = _documents()
    created = _install_reranker_session_factory(
        monkeypatch,
        status_code=503,
        payload={"error": "synthetic rerank failure body"},
        request_id="task3-rerank-error-id",
    )
    reranker = Reranker(
        provider="siliconflow",
        model="synthetic-reranker-model",
        base_url="https://task3-reranker.invalid/v1/rerank",
        api_key="synthetic-rerank-key",
    )
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    try:
        result = reranker.rerank("synthetic query", docs, top_k=2)

        assert [chunk.text for chunk in result] == ["first synthetic document", "second synthetic document"]
        assert all(chunk.rerank_score is None and chunk.score_source == "unavailable" for chunk in result)
        assert len(created) == 1
        assert len(created[0]["adapter"].requests) == 3
        assert _events(caplog) == [
            _expected_event(operation="rerank", status_code=503, request_id="task3-rerank-error-id"),
            _expected_event(operation="rerank", status_code=503, request_id="task3-rerank-error-id"),
            _expected_event(operation="rerank", status_code=503, request_id="task3-rerank-error-id"),
        ]
        assert "task3-rerank-error-id" not in _observer_log_text(caplog)
        assert "synthetic rerank failure body" not in _observer_log_text(caplog)
    finally:
        _assert_reranker_session_closed(reranker, created)
