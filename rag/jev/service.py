"""Public Jev decision facade used by the pipeline."""

from collections.abc import Sequence
from dataclasses import replace

from config import settings
from rag.jev.client import JevClient
from rag.jev.models import JevBatchResult, PreflightDecision
from rag.jev.policy import (
    JevPolicy,
    KbCapabilities,
    PassageSelection,
    RouteDecision,
    choose_route,
    deterministic_route,
    is_self_contained_lookup,
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
            else JevPolicy(
                route_confidence=settings.jev_route_confidence,
                rewrite_probability=settings.jev_rewrite_probability,
            )
        )

    @property
    def client(self) -> JevClient:
        return self._client

    @property
    def policy(self) -> JevPolicy:
        return self._policy

    def route(self, question: str, capabilities: KbCapabilities) -> RouteDecision:
        """Return the deterministic route decision for one question."""
        guarded = deterministic_route(question, capabilities)
        if guarded is not None:
            return guarded
        state = {
            "question": question,
            "capabilities": {
                "structured_data_available": capabilities.has_structured_data,
                "tools_available": list(capabilities.allowed_tools),
                # 字段名让判定器能认出"问题指向表格里的某一列"（见 KbCapabilities 的注释）。
                # 只列调用者有权读取的数据集字段，且已去重截断。
                "structured_fields": list(capabilities.structured_fields),
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
        route = choose_route(result.value, capabilities=capabilities, policy=self._policy)
        if route.should_rewrite and is_self_contained_lookup(question):
            return replace(route, should_rewrite=False, reason=f"{route.reason}+specific_lookup")
        return route

    def select(
        self,
        question: str,
        candidates: Sequence[Chunk],
        *,
        query: str | None = None,
        top_k: int | None = None,
    ) -> PassageSelection | None:
        """Judge ACL-visible candidates and apply passage policy.

        Returns ``None`` when Jev is unavailable so the caller falls back to the legacy reranker.
        """
        if not candidates:
            return PassageSelection(accepted=(), conflicts=(), excluded_ids=(), has_answer_evidence=False)
        # Judging every oversampled candidate makes one unbounded Jev batch; the policy caps it.
        judged = list(candidates)[: self._policy.max_passage_candidates]
        chunk_ids = [f"c{index}" for index in range(len(judged))]
        state = {
            "question": question,
            "query": query if query is not None else question,
            "passages": {
                chunk_id: {"text": candidate.text}
                for chunk_id, candidate in zip(chunk_ids, judged, strict=True)
            },
        }
        result = self._client.judge_passages(state, chunk_ids)
        if not result.available or result.value is None:
            return None
        return select_passages(judged, result.value, policy=self._policy, top_k=top_k)

    def detect_injection(self, texts: Sequence[str]) -> tuple[int, ...] | None:
        """Semantic injection check over retrieved passage texts (A-05).

        This is the same judge the passage policy uses, asked only for its
        ``contains_prompt_injection`` verdict and **without** the majority vote: it is a
        defense-in-depth layer the pipeline can run on any retrieval path, so it must stay a
        single batched call. ``None`` means unavailable (disabled, outage, or malformed
        response) — the caller keeps the passages and relies on the deterministic guard.
        """
        texts = [str(text or "") for text in texts]
        if not texts:
            return ()
        judged = texts[: self._policy.max_passage_candidates]
        chunk_ids = [f"c{index}" for index in range(len(judged))]
        state = {
            "question": "retrieved passages",
            "query": "retrieved passages",
            "passages": {
                chunk_id: {"text": text}
                for chunk_id, text in zip(chunk_ids, judged, strict=True)
            },
        }
        result = self._client.judge_injection(state, chunk_ids)
        if not result.available or result.value is None:
            return None
        flagged = [
            index
            for index, judgment in enumerate(result.value)
            if judgment.prompt_injection >= self._policy.injection_threshold
        ]
        return tuple(flagged)

    def close(self) -> None:
        """Release the underlying adapter client."""
        self._client.close()


__all__ = ["JevDecisionService"]
