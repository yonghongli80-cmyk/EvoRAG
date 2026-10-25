"""Native SDK integration coverage for post-construction upstream hooks."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from config import settings

REQUEST_ID = "native-sdk-request-id-sentinel"
REQUEST_ID_SHA256 = "004a2e04eb469d7c023f4e4db5a770c091a935ce08feac6acbf74659d201fbe3"
BASE_URL = "https://native-client-test.invalid/v1"


class NativeFactories:
    """Test-only constructor factories that return real SDK clients."""

    def __init__(self):
        self.sync_clients = []
        self.async_clients = []
        self.constructor_kwargs = []
        self.requests = []
        self.tracked_streams = []

    def _handler(self, response_factory):
        def handle(request):
            self.requests.append(request)
            response = response_factory(request)
            if isinstance(response.stream, TrackedAsyncStream):
                self.tracked_streams.append(response.stream)
            return response

        return handle

    def sync(self, real_class, response_factory):
        def construct(**kwargs):
            self.constructor_kwargs.append(dict(kwargs))
            http_client = httpx.Client(transport=httpx.MockTransport(self._handler(response_factory)))
            instance = real_class(**kwargs, http_client=http_client)
            self.sync_clients.append(instance)
            return instance

        return construct

    def asynchronous(self, real_class, response_factory):
        def construct(**kwargs):
            self.constructor_kwargs.append(dict(kwargs))
            http_client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler(response_factory)))
            instance = real_class(**kwargs, http_client=http_client)
            self.async_clients.append(instance)
            return instance

        return construct

    def close_sync(self):
        for client in self.sync_clients:
            client.close()
        self.sync_clients.clear()

    @property
    def stream_close_count(self):
        return sum(stream.close_count for stream in self.tracked_streams)

    async def close_async(self):
        for client in self.async_clients:
            await client.close()
        self.async_clients.clear()


class TrackedAsyncStream(httpx.AsyncByteStream):
    def __init__(self, body, error=None):
        self.body = body
        self.error = error
        self.close_count = 0

    async def __aiter__(self):
        yield self.body
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.close_count += 1


def _json_response(request, payload, status=200):
    return httpx.Response(
        status,
        request=request,
        headers={"content-type": "application/json", "x-request-id": REQUEST_ID},
        json=payload,
    )


def _chat_response(request):
    return _json_response(
        request,
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "native answer"}, "finish_reason": "stop"}],
        },
    )


def _embedding_response(request):
    inputs = json.loads(request.content)["input"]
    if isinstance(inputs, str):
        inputs = [inputs]
    return _json_response(
        request,
        {
            "object": "list",
            "model": "embed-test",
            "data": [{"object": "embedding", "index": index, "embedding": [float(index), 0.5]} for index, _ in enumerate(inputs)],
            "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
        },
    )


def _stream_response(request):
    chunks = [
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"test-model","choices":[{"index":0,"delta":{"content":"native "},"finish_reason":null}]}\n\n',
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"test-model","choices":[{"index":0,"delta":{"content":"stream"},"finish_reason":null}]}\n\n',
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"test-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        "data: [DONE]\n\n",
    ]
    return httpx.Response(
        200,
        request=request,
        headers={"content-type": "text/event-stream", "x-request-id": REQUEST_ID},
        stream=TrackedAsyncStream("".join(chunks).encode()),
    )


def _partial_stream_response(request):
    first_chunk = (
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"test-model",'
        '"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
    )
    return httpx.Response(
        200,
        request=request,
        headers={"content-type": "text/event-stream", "x-request-id": REQUEST_ID},
        stream=TrackedAsyncStream(first_chunk.encode(), httpx.ReadError("synthetic stream cut", request=request)),
    )


def _set_provider_settings(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "synthetic-deepseek-key", raising=False)
    monkeypatch.setattr(settings, "deepseek_base_url", BASE_URL, raising=False)
    monkeypatch.setattr(settings, "deepseek_model", "native-test-model", raising=False)
    monkeypatch.setattr(settings, "deepseek_auth_header", "", raising=False)
    monkeypatch.setattr(settings, "bailian_api_key", "synthetic-embedding-key", raising=False)
    monkeypatch.setattr(settings, "bailian_base_url", BASE_URL, raising=False)
    monkeypatch.setattr(settings, "bailian_embed_model", "native-embed-model", raising=False)
    monkeypatch.setattr(settings, "generation_temperature", 0.0, raising=False)


def _events(caplog):
    return [json.loads(record.message) for record in caplog.records if record.name == "rag.upstream_observability"]


def _assert_response_events(caplog, count, operation):
    events = _events(caplog)
    assert len(events) == count
    assert all(event["event"] == "upstream_http_response" for event in events)
    assert all(event["operation"] == operation for event in events)
    assert all(event["status_code"] == 200 for event in events)
    assert all(event["provider_request_id_state"] == "present" for event in events)
    assert all(event["provider_request_id_header"] == "x-request-id" for event in events)
    assert all(event["provider_request_id_sha256"] == REQUEST_ID_SHA256 for event in events)
    rendered = " ".join(
        record.message for record in caplog.records if record.name == "rag.upstream_observability"
    )
    assert REQUEST_ID not in rendered
    assert "synthetic-deepseek-key" not in rendered
    assert "synthetic-embedding-key" not in rendered
    assert "synthetic-pool-key" not in rendered
    assert "synthetic prompt" not in rendered
    assert BASE_URL not in rendered


def test_generator_sync_constructs_real_sdk_client_and_observes_response(monkeypatch, caplog):
    import rag.generator as generator

    _set_provider_settings(monkeypatch)
    monkeypatch.setattr(generator, "client", None)
    generator._breaker.record_success()
    factories = NativeFactories()
    monkeypatch.setattr(generator, "OpenAI", factories.sync(openai.OpenAI, _chat_response))
    caplog.set_level("INFO", logger="rag.upstream_observability")
    try:
        result = generator.generate([{"role": "user", "content": "synthetic prompt"}])
        assert result == "native answer"
        assert len(factories.sync_clients) == 1
        assert len(factories.requests) == 1
        assert factories.constructor_kwargs[0]["base_url"] == BASE_URL
        assert generator._breaker._failure_count == 0
        _assert_response_events(caplog, 1, "generation")
    finally:
        factories.close_sync()
        monkeypatch.setattr(generator, "client", None)
        generator._breaker.record_success()


def test_generator_async_stream_constructs_real_sdk_client_and_preserves_tokens(monkeypatch, caplog):
    import rag.generator as generator

    _set_provider_settings(monkeypatch)
    monkeypatch.setattr(generator, "_async_client", None)
    generator._breaker.record_success()
    factories = NativeFactories()
    monkeypatch.setattr(generator, "AsyncOpenAI", factories.asynchronous(openai.AsyncOpenAI, _stream_response))
    caplog.set_level("INFO", logger="rag.upstream_observability")
    try:
        async def collect():
            return [token async for token in generator.generate_stream([{"role": "user", "content": "prompt"}])]

        assert asyncio.run(collect()) == ["native ", "stream"]
        assert len(factories.async_clients) == 1
        assert len(factories.requests) == 1
        assert factories.stream_close_count == 1
        assert generator._breaker._failure_count == 0
        _assert_response_events(caplog, 1, "generation")
    finally:
        asyncio.run(factories.close_async())
        monkeypatch.setattr(generator, "_async_client", None)
        generator._breaker.record_success()


def test_generator_async_partial_stream_does_not_retry_and_closes_response(monkeypatch, caplog):
    import rag.generator as generator

    _set_provider_settings(monkeypatch)
    monkeypatch.setattr(generator, "_async_client", None)
    generator._breaker.record_success()
    factories = NativeFactories()
    stream_errors = []

    def partial_response(request):
        response = _partial_stream_response(request)
        stream_errors.append(response.stream.error)
        return response

    monkeypatch.setattr(
        generator,
        "AsyncOpenAI",
        factories.asynchronous(openai.AsyncOpenAI, partial_response),
    )
    caplog.set_level("INFO", logger="rag.upstream_observability")
    tokens = []
    try:
        async def collect():
            async for token in generator.generate_stream([{"role": "user", "content": "prompt"}], max_attempts=3):
                tokens.append(token)

        with pytest.raises(httpx.ReadError) as raised:
            asyncio.run(collect())
        assert tokens == ["partial"]
        assert raised.value is stream_errors[0]
        assert len(factories.requests) == 1
        assert factories.stream_close_count == 1
        assert generator._breaker._failure_count == 1
        _assert_response_events(caplog, 1, "generation")
    finally:
        asyncio.run(factories.close_async())
        monkeypatch.setattr(generator, "_async_client", None)
        generator._breaker.record_success()


def test_embedder_observes_each_uncached_batch_but_not_cache_hits(monkeypatch, caplog):
    import rag.embedder as embedder

    _set_provider_settings(monkeypatch)
    monkeypatch.setattr(embedder, "client", None)
    embedder.clear_cache()
    factories = NativeFactories()
    monkeypatch.setattr(embedder, "OpenAI", factories.sync(openai.OpenAI, _embedding_response))
    caplog.set_level("INFO", logger="rag.upstream_observability")
    try:
        texts = [f"native embedding {index}" for index in range(33)]
        first = embedder.embed(texts)
        second = embedder.embed(texts)
        assert len(first) == len(second) == 33
        assert first == second
        assert len(factories.requests) == 2
        assert [len(json.loads(request.content)["input"]) for request in factories.requests] == [32, 1]
        _assert_response_events(caplog, 2, "embedding")
    finally:
        factories.close_sync()
        monkeypatch.setattr(embedder, "client", None)
        embedder.clear_cache()


def test_llm_pool_sync_constructs_real_sdk_client_and_observes_response(monkeypatch, caplog):
    import rag.llm_pool.provider as provider_module
    from rag.llm_pool.models import LLMProviderConfig

    factories = NativeFactories()
    monkeypatch.setattr(provider_module, "OpenAI", factories.sync(openai.OpenAI, _chat_response))
    provider = provider_module.LLMProvider(
        LLMProviderConfig(
            provider_id="test-provider", name="Synthetic", model="native-test-model", base_url=BASE_URL,
            api_key="synthetic-pool-key", auth_header="", weight=1, max_concurrency=1, rpm_limit=60,
            timeout_seconds=5.0, is_active=True,
        )
    )
    caplog.set_level("INFO", logger="rag.upstream_observability")
    try:
        assert provider.generate([{"role": "user", "content": "prompt"}]) == "native answer"
        assert len(factories.requests) == 1
        assert provider.circuit_breaker._failure_count == 0
        _assert_response_events(caplog, 1, "generation")
    finally:
        factories.close_sync()


def test_llm_pool_async_stream_constructs_real_sdk_client_and_preserves_tokens(monkeypatch, caplog):
    import rag.llm_pool.provider as provider_module
    from rag.llm_pool.models import LLMProviderConfig

    factories = NativeFactories()
    monkeypatch.setattr(provider_module, "AsyncOpenAI", factories.asynchronous(openai.AsyncOpenAI, _stream_response))
    provider = provider_module.LLMProvider(
        LLMProviderConfig(
            provider_id="test-provider", name="Synthetic", model="native-test-model", base_url=BASE_URL,
            api_key="synthetic-pool-key", auth_header="", weight=1, max_concurrency=1, rpm_limit=60,
            timeout_seconds=5.0, is_active=True,
        )
    )
    caplog.set_level("INFO", logger="rag.upstream_observability")
    try:
        async def collect():
            return [token async for token in provider.generate_stream([{"role": "user", "content": "prompt"}])]

        assert asyncio.run(collect()) == ["native ", "stream"]
        assert len(factories.requests) == 1
        assert factories.stream_close_count == 1
        assert provider.circuit_breaker._failure_count == 0
        _assert_response_events(caplog, 1, "generation")
    finally:
        asyncio.run(factories.close_async())


@pytest.mark.parametrize(
    ("module_name", "content"),
    [
        ("rag.evolution.candidate_generator", '{"candidate":"synthetic"}'),
        ("rag.evolution.diagnoser", '{"diagnosis":"synthetic"}'),
        ("rag.evolution.patch_generator", "diff --git a/a.py b/a.py\n"),
    ],
)
def test_evolution_default_model_call_constructs_real_sdk_client_and_observes(
    module_name, content, monkeypatch, caplog
):
    import importlib

    module = importlib.import_module(module_name)
    _set_provider_settings(monkeypatch)
    factories = NativeFactories()
    constructor = factories.sync(openai.OpenAI, lambda request: _json_response(
        request,
        {
            "id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        },
    ))
    # Isolate this module's imported SDK namespace. Patching openai.OpenAI globally
    # would also patch the saved real class and recurse inside the factory.
    monkeypatch.setattr(
        module,
        "openai",
        SimpleNamespace(OpenAI=constructor, APIStatusError=openai.APIStatusError, RateLimitError=openai.RateLimitError),
    )
    caplog.set_level("INFO", logger="rag.upstream_observability")
    try:
        result = module._default_model_call()([{"role": "user", "content": "synthetic prompt"}])
        assert result == content
        assert len(factories.requests) == 1
        assert len(factories.sync_clients) == 1
        _assert_response_events(caplog, 1, "generation")
    finally:
        factories.close_sync()
