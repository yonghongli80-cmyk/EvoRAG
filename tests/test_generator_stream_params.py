import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_stream_passes_temperature_and_records_each_timeout():
    import rag.generator as generator

    async_client = MagicMock()
    async_client.chat.completions.create = AsyncMock(side_effect=TimeoutError("slow"))
    breaker = MagicMock()
    breaker.allow_request.return_value = True

    with patch.object(generator, "_async_client", async_client), patch.object(
        generator, "_breaker", breaker
    ), patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(TimeoutError):
            async for _ in generator.generate_stream([], temperature=0.7, max_attempts=2):
                pass

    assert async_client.chat.completions.create.call_args.kwargs["temperature"] == 0.7
    assert breaker.record_failure.call_count == 2
