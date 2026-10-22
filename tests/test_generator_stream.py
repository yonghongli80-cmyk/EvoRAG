"""流式生成器测试。"""
import asyncio
import inspect
from unittest.mock import MagicMock, patch


async def _coroutine_wrap(async_gen):
    """Wrap async generator in a coroutine (mimics AsyncOpenAI create() behavior)."""
    return async_gen


def test_generate_stream_returns_async_generator():
    """generate_stream 应该返回一个 async generator。"""
    from rag.generator import generate_stream
    result = generate_stream([{"role": "user", "content": "hello"}])
    assert inspect.isasyncgen(result), f"应该是 async generator，实际是 {type(result)}"


def test_generate_stream_yields_tokens():
    """generate_stream 应该逐个 yield token。"""
    import rag.generator as _gen
    _gen._async_client = None
    from rag.generator import generate_stream
    mock_chunks = []
    for text in ["你好", "世界"]:
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = text
        mock_chunks.append(chunk)
    final = MagicMock()
    final.choices = [MagicMock()]
    final.choices[0].delta.content = None
    mock_chunks.append(final)

    async def mock_stream():
        for c in mock_chunks:
            yield c

    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(
        return_value=_coroutine_wrap(mock_stream())
    )

    with patch("rag.generator.AsyncOpenAI", return_value=mock_client):
        tokens = []

        async def collect():
            async for token in generate_stream([{"role": "user", "content": "hello"}]):
                tokens.append(token)

        asyncio.run(collect())

    assert tokens == ["你好", "世界"]


def test_generate_stream_strips_reasoning_content():
    """generate_stream 应该过滤 reasoning_content 字段。"""
    import rag.generator as _gen
    _gen._async_client = None
    from rag.generator import generate_stream
    messages = [{"role": "user", "content": "hello", "reasoning_content": "thinking..."}]
    mock_chunk = MagicMock()
    mock_chunk.choices = [MagicMock()]
    mock_chunk.choices[0].delta.content = "hi"
    final = MagicMock()
    final.choices = [MagicMock()]
    final.choices[0].delta.content = None

    async def mock_stream():
        yield mock_chunk
        yield final

    mock_client = MagicMock()
    captured_messages = None

    def capture_create(**kwargs):
        nonlocal captured_messages
        captured_messages = kwargs.get("messages")
        return _coroutine_wrap(mock_stream())

    mock_client.chat.completions.create = capture_create

    with patch("rag.generator.AsyncOpenAI", return_value=mock_client):
        tokens = []

        async def collect():
            async for token in generate_stream(messages):
                tokens.append(token)

        asyncio.run(collect())

    assert captured_messages is not None
    for msg in captured_messages:
        assert "reasoning_content" not in msg


def test_generate_stream_raises_circuit_open_when_breaker_is_open():
    """熔断时必须抛可识别失败，不能把降级文案当成 token 流出去。"""
    import pytest

    from rag.generator import _breaker, generate_stream
    from rag.resilience import CircuitOpenError

    for _ in range(5):
        _breaker.record_failure()

    tokens = []

    async def collect():
        async for token in generate_stream([{"role": "user", "content": "hello"}]):
            tokens.append(token)

    with pytest.raises(CircuitOpenError):
        asyncio.run(collect())

    assert tokens == []
    _breaker.record_success()


def test_generate_stream_does_not_retry_after_yielding_tokens():
    """一旦已 yield token，发生异常时不可从头重试（避免内容重复）。"""
    import pytest

    import rag.generator as _gen

    _gen._async_client = None
    _gen._breaker.record_success()

    call_count = 0

    async def faulty_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "第一段"
        yield chunk
        raise ConnectionError("network dropped mid-stream")

    mock_client = MagicMock()

    def mock_create(**kwargs):
        nonlocal call_count
        call_count += 1
        return _coroutine_wrap(faulty_stream())

    mock_client.chat.completions.create = mock_create

    with patch("rag.generator.AsyncOpenAI", return_value=mock_client):
        tokens = []

        async def collect():
            async for token in _gen.generate_stream([{"role": "user", "content": "hi"}], max_attempts=3):
                tokens.append(token)

        with pytest.raises(ConnectionError):
            asyncio.run(collect())

    # 必须只调用了 1 次，不可重试从头重复发
    assert call_count == 1
    assert tokens == ["第一段"]

