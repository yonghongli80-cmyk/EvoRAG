"""Re-ranking module using SiliconFlow Rerank API."""

import logging
import math
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
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = requests.Session()
            self._client.headers.update(
                {
                    "Authorization": f"Bearer {settings.bailian_api_key}",
                    "Content-Type": "application/json",
                }
            )
        return self._client

    @retry(
        max_attempts=3,
        backoff_base=1.0,
        retryable_exceptions=(requests.RequestException, TimeoutError, ConnectionError),
    )
    def _call_rerank_api(self, query: str, texts: list[str], top_k: int) -> list[dict]:
        client = self._get_client()
        payload = {
            "model": RERANK_MODEL,
            "query": query,
            "documents": texts,
            "top_n": top_k,
            "return_documents": False,
        }
        resp = client.post(RERANK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        try:
            return resp.json()["results"]
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"Unexpected rerank API response: {e}") from e

    def rerank(self, query: str, documents: list[Chunk], top_k: int | None = None) -> list[Chunk]:
        if not documents:
            return []
        if top_k is None:
            top_k = settings.rerank_top_k
        if not _breaker.allow_request():
            logger.warning("Circuit breaker open — falling back to original document order")
            return [replace(document, rerank_score=None, score_source="unavailable") for document in documents[:top_k]]
        texts = [c.text for c in documents]
        try:
            results = self._call_rerank_api(query, texts, min(top_k, len(documents)))
            ranked = []
            for result in results:
                score = float(result["relevance_score"])
                if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                    raise ValueError("reranker relevance_score must be finite and within [0, 1]")
                ranked.append(replace(documents[result["index"]], rerank_score=score, score_source="reranker"))
            _breaker.record_success()
            return sorted(ranked, key=lambda document: -document.rerank_score)[:top_k]
        except Exception:
            _breaker.record_failure()
            logger.warning("Rerank failed — falling back to original document order", exc_info=True)
            return [replace(document, rerank_score=None, score_source="unavailable") for document in documents[:top_k]]
