"""OpenAI SDK transport/status failures should be retried by the embedder."""

from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from rag.embedder import embed


def _embedding_response():
    response = MagicMock()
    response.data = [MagicMock(embedding=[0.1, 0.2])]
    return response


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=httpx.Request("POST", "https://embed.test")),
        openai.RateLimitError(
            "rate limited",
            response=httpx.Response(
                429,
                request=httpx.Request("POST", "https://embed.test"),
            ),
            body={"error": "rate_limit"},
        ),
    ],
)
def test_embed_retries_retryable_openai_sdk_errors(error):
    mock_client = MagicMock()
    mock_client.embeddings.create.side_effect = [error, _embedding_response()]

    with patch("rag.embedder.client", mock_client), patch("rag.resilience.time.sleep"):
        result = embed([f"retry-{type(error).__name__}"])

    assert result == [[0.1, 0.2]]
    assert mock_client.embeddings.create.call_count == 2
