"""Regression tests for upstream OpenAI stream ownership and cleanup."""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from rag.llm_pool.concurrency import DistributedConcurrencyGate
from rag.llm_pool.models import LLMProviderConfig
from rag.llm_pool.provider import LLMProvider
from rag.llm_pool.router import LLMPoolRouter


class _SSEBody(httpx.AsyncByteStream):
    def __init__(self, *, wait_after_first=False, fail_before_first=False, fail_after_first=False):
        self.closed = False
        self.wait_after_first = wait_after_first
        self.fail_before_first = fail_before_first
        self.fail_after_first = fail_after_first
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        if self.fail_before_first:
            raise OSError("synthetic read failure before first token")
        yield self._frame("first token")
        if self.fail_after_first:
            raise OSError("synthetic read failure after first token")
        if self.wait_after_first:
            self.waiting.set()
            await asyncio.Event().wait()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True

    @staticmethod
    def _frame(token):
        payload = {"choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}]}
        return f"data: {json.dumps(payload)}\n\n".encode()


def _client_for(body):
    return _client_for_bodies([body])


def _client_for_bodies(bodies):
    responses = iter(bodies)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=next(responses)
        )
    )
    http_client = httpx.AsyncClient(transport=transport)
    return AsyncOpenAI(api_key="test-key", base_url="https://unit.test/v1", http_client=http_client)


def _provider(client):
    config = LLMProviderConfig(
        provider_id="test", name="Test", model="test-model", base_url="https://unit.test/v1",
        api_key="test-key", weight=1, max_concurrency=1, rpm_limit=60, timeout_seconds=5.0,
        is_active=True,
    )
    provider = LLMProvider(config)
    provider._async_client = client
    return provider


@pytest.mark.asyncio
async def test_legacy_stream_closes_real_sdk_response_when_consumer_closes_after_token(monkeypatch):
    import rag.generator as generator

    body = _SSEBody()
    client = _client_for(body)
    monkeypatch.setattr(generator, "_async_client", client)
    stream = generator.generate_stream([{"role": "user", "content": "hello"}])
    try:
        assert await anext(stream) == "first token"
        await stream.aclose()
        assert body.closed is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_provider_closes_real_sdk_response_when_consumer_closes_after_token():
    body = _SSEBody()
    client = _client_for(body)
    stream = _provider(client).generate_stream([{"role": "user", "content": "hello"}])
    try:
        assert await anext(stream) == "first token"
        await stream.aclose()
        assert body.closed is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_provider_closes_response_and_does_not_count_consumer_cancellation_as_failure():
    body = _SSEBody(wait_after_first=True)
    client = _client_for(body)
    provider = _provider(client)
    stream = provider.generate_stream([{"role": "user", "content": "hello"}])

    try:
        assert await anext(stream) == "first token"
        waiting = asyncio.create_task(anext(stream))
        await asyncio.wait_for(body.waiting.wait(), timeout=1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        assert body.closed is True
        assert provider.circuit_breaker._failure_count == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_stream_closes_response_and_does_not_count_waiting_cancellation_as_failure(monkeypatch):
    import rag.generator as generator

    body = _SSEBody(wait_after_first=True)
    client = _client_for(body)
    monkeypatch.setattr(generator, "_async_client", client)
    failure_count = generator._breaker._failure_count
    stream = generator.generate_stream([{"role": "user", "content": "hello"}])
    try:
        assert await anext(stream) == "first token"
        waiting = asyncio.create_task(anext(stream))
        await asyncio.wait_for(body.waiting.wait(), timeout=1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        assert body.closed is True
        assert generator._breaker._failure_count == failure_count
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_stream_closes_response_before_retrying_a_pre_token_read_failure(monkeypatch):
    import rag.generator as generator

    failed_body = _SSEBody(fail_before_first=True)
    successful_body = _SSEBody()
    client = _client_for_bodies([failed_body, successful_body])
    monkeypatch.setattr(generator, "_async_client", client)
    stream = generator.generate_stream([{"role": "user", "content": "hello"}], max_attempts=2)
    try:
        assert [token async for token in stream] == ["first token"]
        assert failed_body.closed is True
        assert successful_body.closed is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_provider_closes_real_sdk_response_when_stream_fails_after_a_token():
    body = _SSEBody(fail_after_first=True)
    client = _client_for(body)
    provider = _provider(client)
    stream = provider.generate_stream([{"role": "user", "content": "hello"}])
    try:
        assert await anext(stream) == "first token"
        with pytest.raises(OSError, match="synthetic read failure after first token"):
            await anext(stream)
        assert body.closed is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_provider_closes_real_sdk_response_after_normal_completion():
    body = _SSEBody()
    client = _client_for(body)
    stream = _provider(client).generate_stream([{"role": "user", "content": "hello"}])
    try:
        assert [token async for token in stream] == ["first token"]
        assert body.closed is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_router_close_reaches_sdk_response_and_releases_provider_gate():
    body = _SSEBody()
    client = _client_for(body)
    provider = _provider(client)
    gate = DistributedConcurrencyGate()
    router = LLMPoolRouter([provider], gate, default_provider_id="test")
    stream = router.generate_stream_balanced(
        [{"role": "user", "content": "hello"}], preferred_provider_id="test"
    )

    try:
        assert (await anext(stream))["content"] == "first token"
        await stream.aclose()
        assert body.closed is True
        assert gate.get_inflight("test") == 0
    finally:
        await client.close()
