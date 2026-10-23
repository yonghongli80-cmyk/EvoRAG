"""Re-ranking module using SiliconFlow Rerank API."""

import logging
import math
import threading
from dataclasses import replace

import requests

from config import settings
from rag.models import Chunk
from rag.resilience import CircuitBreaker, retry

logger = logging.getLogger(__name__)

_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

RERANK_URL = "https://api.siliconflow.cn/v1/rerank"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


class Reranker:
    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self._explicit_provider = provider
        self._explicit_model = model
        self._explicit_url = base_url
        self._explicit_api_key = api_key
        self._client = None
        self._client_lock = threading.Lock()

    @property
    def provider(self) -> str:
        prov = self._explicit_provider or settings.rerank_provider
        return prov.lower().strip()

    @property
    def model(self) -> str:
        if self._explicit_model:
            return self._explicit_model
        if settings.rerank_model:
            return settings.rerank_model
        prov = self.provider
        if prov == "siliconflow":
            return settings.siliconflow_rerank_model
        if prov == "bailian":
            return settings.bailian_rerank_model
        if prov == "local":
            return settings.local_rerank_model
        raise ValueError(f"不支持的重排序提供商: {prov}")

    @property
    def url(self) -> str:
        if self._explicit_url:
            return self._explicit_url
        if settings.rerank_base_url:
            return settings.rerank_base_url
        prov = self.provider
        if prov == "siliconflow":
            return settings.siliconflow_rerank_url
        if prov == "bailian":
            return settings.bailian_rerank_url
        if prov == "local":
            return settings.local_rerank_url
        raise ValueError(f"不支持的重排序提供商: {prov}")

    @property
    def api_key(self) -> str:
        if self._explicit_api_key:
            return self._explicit_api_key
        if settings.rerank_api_key:
            return settings.rerank_api_key
        prov = self.provider
        if prov in ("siliconflow", "bailian"):
            return settings.bailian_api_key
        return ""

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = requests.Session()
                    headers = {"Content-Type": "application/json"}
                    key = self.api_key
                    if key:
                        headers["Authorization"] = f"Bearer {key}"
                    self._client.headers.update(headers)
        return self._client

    def close(self) -> None:
        """Release the process-local requests session (idempotent)."""
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    @retry(
        max_attempts=3,
        backoff_base=1.0,
        retryable_exceptions=(requests.RequestException, TimeoutError, ConnectionError),
    )
    def _call_rerank_api(self, query: str, texts: list[str], top_k: int) -> list[dict]:
        client = self._get_client()
        url = self.url
        model = self.model
        prov = self.provider

        if prov == "bailian" and "/services/rerank/" in url:
            payload = {
                "model": model,
                "input": {
                    "query": query,
                    "documents": texts,
                },
                "parameters": {
                    "top_n": top_k,
                    "return_documents": False,
                },
            }
        else:
            payload = {
                "model": model,
                "query": query,
                "documents": texts,
                "top_n": top_k,
                "return_documents": False,
            }

        resp = client.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        try:
            data = resp.json()
            if "results" in data:
                return data["results"]
            if "output" in data and "results" in data["output"]:
                return data["output"]["results"]
            raise KeyError("results")
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"Unexpected rerank API response: {e}") from e

    def rerank(self, query: str, documents: list[Chunk], top_k: int | None = None) -> list[Chunk]:
        if not documents:
            return []
        if top_k is None:
            top_k = settings.rerank_top_k
        # Fallback paths must not truncate the recalled candidate set: dropping
        # the tail is what keeps alias-query chunks (brand rows answering
        # "第三方/副厂配件") out of judging and generation.
        if not _breaker.allow_request():
            logger.warning("Circuit breaker open — falling back to original document order")
            return [replace(document, rerank_score=None, score_source="unavailable") for document in documents]
        texts = [c.text for c in documents]
        try:
            results = self._call_rerank_api(query, texts, min(top_k, len(documents)))
            ranked = []
            for result in results:
                score = float(result["relevance_score"])
                if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                    raise ValueError("reranker relevance_score must be finite and within [0, 1]")
                ranked.append(replace(documents[result["index"]], rerank_score=score, score_source="reranker"))
            if not ranked:
                raise RuntimeError("Reranker returned no usable results")
            _breaker.record_success()
            return sorted(ranked, key=lambda document: -document.rerank_score)[:top_k]
        except Exception:
            _breaker.record_failure()
            logger.warning("Rerank failed — falling back to original document order", exc_info=True)
            return [replace(document, rerank_score=None, score_source="unavailable") for document in documents]


def merge_ranked_chunks(primary: list[Chunk], secondary: list[Chunk]) -> list[Chunk]:
    """Merge two rerank passes over the same candidates, keeping the higher score per chunk."""

    def score_of(chunk: Chunk) -> float:
        return chunk.rerank_score if chunk.rerank_score is not None else float("-inf")

    merged: dict[tuple[str, int, str], Chunk] = {}
    first_seen: list[tuple[str, int, str]] = []
    for chunk in (*primary, *secondary):
        key = (chunk.doc_name, chunk.chunk_index, chunk.text)
        current = merged.get(key)
        if current is None:
            merged[key] = chunk
            first_seen.append(key)
        elif score_of(chunk) > score_of(current):
            merged[key] = chunk

    scored: list[Chunk] = []
    unscored: list[Chunk] = []
    for key in first_seen:
        chunk = merged[key]
        if chunk.rerank_score is None:
            unscored.append(chunk)
        else:
            scored.append(chunk)
    scored.sort(key=lambda chunk: -score_of(chunk))
    return scored + unscored
