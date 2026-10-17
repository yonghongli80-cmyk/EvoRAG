"""TDD unit tests for LLMProvider (independent circuit breaker, stream generator, and headers)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.llm_pool.models import LLMProviderConfig
from rag.llm_pool.provider import LLMProvider


def _make_config(provider_id="deepseek", name="DeepSeek", model="deepseek-chat"):
    return LLMProviderConfig(
        provider_id=provider_id,
        name=name,
        model=model,
        base_url="https://api.example.com/v1",
        api_key="sk-test-123",
        auth_header="x-api-key" if provider_id == "custom" else "",
        weight=2,
        max_concurrency=5,
        rpm_limit=60,
        timeout_seconds=15.0,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_llm_provider_initialization_and_headers():
    """Verify custom auth headers and standard Bearer headers."""
    prov_custom = LLMProvider(_make_config(provider_id="custom"))
    assert prov_custom.default_headers() == {"x-api-key": "sk-test-123"}

    prov_std = LLMProvider(_make_config(provider_id="deepseek"))
    assert prov_std.default_headers() is None


@pytest.mark.asyncio
async def test_llm_provider_streaming_tokens():
    """Verify streaming token generation."""
    provider = LLMProvider(_make_config())

    chunk1 = MagicMock()
    chunk1.choices = [MagicMock(delta=MagicMock(content="Hello"))]
    chunk2 = MagicMock()
    chunk2.choices = [MagicMock(delta=MagicMock(content=" world!"))]

    async def fake_stream():
        yield chunk1
        yield chunk2

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())
    provider._async_client = mock_client

    tokens = []
    async for token in provider.generate_stream([{"role": "user", "content": "hi"}]):
        tokens.append(token)

    assert "".join(tokens) == "Hello world!"
    assert provider.circuit_breaker.allow_request() is True
    assert provider.circuit_breaker._failure_count == 0


@pytest.mark.asyncio
async def test_llm_provider_circuit_breaker_isolation():
    """Failures on Provider A must not affect Provider B's circuit breaker."""
    prov_a = LLMProvider(_make_config(provider_id="model_a"))
    prov_b = LLMProvider(_make_config(provider_id="model_b"))

    mock_failing_client = MagicMock()
    mock_failing_client.chat.completions.create = AsyncMock(side_effect=TimeoutError("Connection timed out"))
    prov_a._async_client = mock_failing_client

    # 让 prov_a 连续失败 5 次触发熔断
    for _ in range(5):
        with pytest.raises(TimeoutError):
            async for _ in prov_a.generate_stream([{"role": "user", "content": "test"}]):
                pass

    # prov_a 已熔断
    assert not prov_a.circuit_breaker.allow_request()
    assert not prov_a.is_available()

    # prov_b 必须依然健康可用！
    assert prov_b.circuit_breaker.allow_request()
    assert prov_b.is_available()
