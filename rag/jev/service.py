"""Public Jev decision facade used by the pipeline."""

from collections.abc import Sequence

from config import settings
from rag.jev.client import JevClient
from rag.jev.models import JevBatchResult, PreflightDecision
from rag.jev.policy import (
    JevPolicy,
    KbCapabilities,
    PassageSelection,
    RouteDecision,
    choose_route,
    select_passages,
)
from rag.models import Chunk


class JevDecisionService:
    """Runs the preflight call and applies deterministic policy to the result."""

    def __init__(self, client: JevClient | None = None, policy: JevPolicy | None = None):
        self._client = client if client is not None else JevClient()
        self._policy = (
            policy
            if policy is not None
            else JevPolicy(route_confidence=settings.jev_route_confidence)
        )

    @property
    def client(self) -> JevClient:
        return self._client

    @property
    def policy(self) -> JevPolicy:
        return self._policy

    def route(self, question: str, capabilities: KbCapabilities) -> RouteDecision:
        """Return the deterministic route decision for one question."""
        state = {
            "question": question,
            "capabilities": {
                "structured_data_available": capabilities.has_structured_data,
                "tools_available": list(capabilities.allowed_tools),
            },
        }
        result: JevBatchResult[PreflightDecision] = self._client.preflight(state)
        if not result.available or result.value is None:
            return RouteDecision(
                route="legacy",
                should_rewrite=False,
                reason=f"preflight_unavailable:{result.failure_kind}",
                preflight=None,
            )
        return choose_route(result.value, capabilities=capabilities, policy=self._policy)

    def select(
        self,
        question: str,
        query: str,
        candidates: Sequence[Chunk],
        top_k: int | None = None,
    ) -> PassageSelection | None:
        """Judge ACL-visible candidates and apply passage policy.

        Returns ``None`` when Jev is unavailable so the caller falls back to the legacy reranker.
        """
        if not candidates:
            return PassageSelection(accepted=(), conflicts=(), excluded_ids=(), has_answer_evidence=False)
        chunk_ids = [f"c{index}" for index in range(len(candidates))]
        state = {
            "question": question,
            "query": query,
            "passages": {
                chunk_id: {"text": candidate.text}
                for chunk_id, candidate in zip(chunk_ids, candidates, strict=True)
            },
        }
        result = self._client.judge_passages(state, chunk_ids)
        if not result.available or result.value is None:
            return None
        return select_passages(candidates, result.value, policy=self._policy, top_k=top_k)

    def close(self) -> None:
        """Release the underlying adapter client."""
        self._client.close()


__all__ = ["JevDecisionService"]
