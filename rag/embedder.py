import threading
from functools import lru_cache

import openai
from openai import OpenAI

from config import settings
from rag.resilience import retry

client = None
_client_lock = threading.Lock()


@retry(
    max_attempts=3,
    backoff_base=1.0,
    retryable_exceptions=(
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.RateLimitError,
        openai.APIStatusError,
        TimeoutError,
        ConnectionError,
    ),
)
@lru_cache(maxsize=512)
def _embed_single(text: str) -> tuple:
    """Cache a single text embedding. Returns tuple (hashable)."""
    response = client.embeddings.create(
        model=settings.bailian_embed_model,
        input=[text],
    )
    return tuple(response.data[0].embedding)


def embed(texts: list[str]) -> list[list[float]]:
    global client
    if client is None:
        with _client_lock:
            if client is None:
                client = OpenAI(
                    api_key=settings.bailian_api_key,
                    base_url=settings.bailian_base_url,
                    timeout=30.0,
                )
    results = []
    for text in texts:
        cached = _embed_single(text)
        results.append(list(cached))
    return results


def clear_cache():
    """Clear the LRU embedding cache."""
    _embed_single.cache_clear()
