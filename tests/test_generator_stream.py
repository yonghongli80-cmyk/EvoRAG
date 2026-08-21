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


def test_generate_stream_respects_circuit_breaker():
    """熔断时 generate_stream 应该 yield 降级提示。"""
    from rag.generator import _breaker, generate_stream
    for _ in range(5):
        _breaker.record_failure()

    tokens = []

    async def collect():
        async for token in generate_stream([{"role": "user", "content": "hello"}]):
            tokens.append(token)

    asyncio.run(collect())

    assert len(tokens) > 0
    assert "系统繁忙" in "".join(tokens)
    _breaker.record_success()
