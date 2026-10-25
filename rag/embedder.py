import threading
from collections import OrderedDict

import openai
from openai import OpenAI

from config import settings
from rag.resilience import retry
from rag.upstream_observability import attach_openai_observer

client = None
_client_lock = threading.Lock()

_CACHE_MAX_SIZE = 512
_cache_lock = threading.Lock()
_cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()

_BATCH_SIZE = 32


def _get_from_cache(text: str) -> tuple[float, ...] | None:
    with _cache_lock:
        if text in _cache:
            _cache.move_to_end(text)
            return _cache[text]
    return None


def _put_in_cache(text: str, emb: tuple[float, ...]) -> None:
    with _cache_lock:
        if text in _cache:
            _cache.move_to_end(text)
        else:
            if len(_cache) >= _CACHE_MAX_SIZE:
                _cache.popitem(last=False)
            _cache[text] = emb


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
def _call_embeddings_api(batch_texts: list[str]) -> list[tuple[float, ...]]:
    response = client.embeddings.create(
        model=settings.bailian_embed_model,
        input=batch_texts,
    )
    return [tuple(item.embedding) for item in response.data]


def _embed_single(text: str) -> tuple[float, ...]:
    """Cache a single text embedding. Returns tuple (hashable)."""
    cached = _get_from_cache(text)
    if cached is not None:
        return cached
    emb = _call_embeddings_api([text])[0]
    _put_in_cache(text, emb)
    return emb


def clear_cache() -> None:
    """Clear the LRU embedding cache."""
    with _cache_lock:
        _cache.clear()


_embed_single.cache_clear = clear_cache


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
                attach_openai_observer(client)

    if not texts:
        return []

    results: list[list[float] | None] = [None] * len(texts)
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []

    for i, text in enumerate(texts):
        cached = _get_from_cache(text)
        if cached is not None:
            results[i] = list(cached)
        else:
            uncached_indices.append(i)
            uncached_texts.append(text)

    if uncached_texts:
        for offset in range(0, len(uncached_texts), _BATCH_SIZE):
            chunk_texts = uncached_texts[offset : offset + _BATCH_SIZE]
            chunk_indices = uncached_indices[offset : offset + _BATCH_SIZE]
            embeddings = _call_embeddings_api(chunk_texts)
            for idx, text, emb in zip(chunk_indices, chunk_texts, embeddings, strict=False):
                _put_in_cache(text, emb)
                results[idx] = list(emb)

    return results  # type: ignore[return-value]
