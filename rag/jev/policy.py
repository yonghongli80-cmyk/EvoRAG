"""Deterministic route and rewrite policy.

All thresholds live in :class:`JevPolicy`, calibrated against project data rather than copied
from documentation. Jev never encodes route policy in prompt prose; this module owns routing
and passage selection. Request-local chunk IDs follow the ``c{index}`` convention shared with
:mod:`rag.jev.service`.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from rag.jev.models import PassageJudgment, PreflightDecision
from rag.models import Chunk

RouteKind = Literal["structured", "rag", "tool", "clarification", "legacy"]


@dataclass(frozen=True)
class KbCapabilities:
    """Permission-safe capability summary shared with the preflight state."""

    has_structured_data: bool = False
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class JevPolicy:
    """Confidence, rewrite, and passage thresholds used by the deterministic policy."""

    route_confidence: float = 0.75
    rewrite_probability: float = 0.5
    injection_threshold: float = 0.7
    contradiction_threshold: float = 0.5
    evidence_threshold: float = 0.5
    relevance_threshold: float = 0.5
    # Calibrated candidate budget for the batched passage judgment: one Jev request per
    # candidate chunk, so an unbounded batch turns into an unbounded latency tail.
    max_passage_candidates: int = 12


@dataclass(frozen=True)
class RouteDecision:
    """The deterministic route choice derived from one preflight decision."""

    route: RouteKind
    should_rewrite: bool
    reason: str
    preflight: PreflightDecision | None = None


@dataclass(frozen=True)
class PassageSelection:
    """Deterministic passage outcome after Jev judgments are applied.

    ``question_set_version`` names the versioned question set the applied
    judgments came from (``""`` when the batch named none), so the pipeline can
    stamp a response with the passages version that served it — the preflight
    side already reports its own version through ``PreflightDecision``.
    """

    accepted: tuple[Chunk, ...]
    conflicts: tuple[Chunk, ...]
    excluded_ids: tuple[str, ...]
    has_answer_evidence: bool
    question_set_version: str = ""
    #: What the judge said about the candidates it saw, counted **before** the
    #: policy turns those judgments into a selection: one candidate can cross
    #: several thresholds at once (relevant *and* injected, for instance), and the
    #: raw counts are what let a later diagnosis tell "nothing was retrieved" apart
    #: from "candidates were retrieved and judged relevant, yet none carried answer
    #: evidence" — the second is a judging defect, not a content gap.
    judged: int = 0
    relevant: int = 0
    with_evidence: int = 0
    contradicted: int = 0
    injected: int = 0


def choose_route(
    decision: PreflightDecision,
    *,
    capabilities: KbCapabilities | None = None,
    policy: JevPolicy | None = None,
    threshold: float | None = None,
) -> RouteDecision:
    """Route deterministically from a preflight decision.

    Low confidence never selects a high-risk route: ``tool_task`` and uncertain structured
    intents fall back to the legacy router, while uncertain clarification and uncertain
    retrieval default to RAG, the low-risk path. ``capabilities=None`` means the structured
    capability was not evaluated.
    """
    effective = policy or JevPolicy()
    route_confidence = effective.route_confidence if threshold is None else threshold
    confident = decision.intent_confidence >= route_confidence
    should_rewrite = decision.rewrite_probability >= effective.rewrite_probability

    if decision.intent == "structured_query":
        if not confident:
            return RouteDecision("legacy", should_rewrite, "structured_low_confidence", decision)
        if capabilities is not None and not capabilities.has_structured_data:
            return RouteDecision("legacy", should_rewrite, "structured_capability_missing", decision)
        return RouteDecision("structured", should_rewrite, "structured_confident", decision)

    if decision.intent == "tool_task":
        if not confident:
            return RouteDecision("legacy", should_rewrite, "tool_low_confidence", decision)
        return RouteDecision("tool", should_rewrite, "tool_confident", decision)

    if decision.intent == "clarification":
        if not confident:
            return RouteDecision("rag", should_rewrite, "clarification_low_confidence_defaults_rag", decision)
        return RouteDecision("clarification", should_rewrite, "clarification_confident", decision)

    if not confident:
        # RAG is the low-risk default for an uncertain retrieval intent.
        return RouteDecision("rag", should_rewrite, "retrieval_low_confidence_defaults_rag", decision)
    return RouteDecision("rag", should_rewrite, "retrieval_confident", decision)


def select_passages(
    candidates: Sequence[Chunk],
    judgments: Sequence[PassageJudgment],
    *,
    policy: JevPolicy | None = None,
    top_k: int | None = None,
) -> PassageSelection:
    """Apply deterministic passage policy to Jev judgments.

    Prompt-injected passages are excluded first, even when they also answer the question.
    Contradicting passages are kept in a separate conflict set. Answer evidence ranks before
    background relevance, ties keep request order, and accepted results are capped at ``top_k``.
    A missing or duplicated judgment rejects the whole batch instead of partially trusting it.
    """
    effective = policy or JevPolicy()
    by_chunk_id = _judgments_by_chunk_id(judgments)
    version = _uniform_question_set_version(judgments)

    excluded: list[str] = []
    conflicts: list[Chunk] = []
    evidence: list[tuple[float, float, int, Chunk]] = []
    background: list[tuple[float, int, Chunk]] = []
    relevant = with_evidence = contradicted = injected = 0
    for index, candidate in enumerate(candidates):
        chunk_id = f"c{index}"
        judgment = by_chunk_id.get(chunk_id)
        if judgment is None:
            raise ValueError(f"missing judgment for chunk {chunk_id}")
        is_injected = judgment.prompt_injection >= effective.injection_threshold
        is_contradiction = judgment.contradiction >= effective.contradiction_threshold
        is_evidence = judgment.answer_evidence >= effective.evidence_threshold
        is_relevant = judgment.relevance >= effective.relevance_threshold
        # Raw judge statistics first: the policy's exclusions must not hide what the
        # judge actually said about the candidates it was given.
        relevant += 1 if is_relevant else 0
        with_evidence += 1 if is_evidence else 0
        contradicted += 1 if is_contradiction else 0
        injected += 1 if is_injected else 0
        if is_injected:
            excluded.append(chunk_id)
            continue
        marked = _with_jev_provenance(candidate, judgment, effective)
        if is_contradiction:
            conflicts.append(marked)
        elif is_evidence:
            evidence.append((-judgment.answer_evidence, -judgment.relevance, index, marked))
        elif is_relevant:
            background.append((-judgment.relevance, index, marked))

    accepted = [item[3] for item in sorted(evidence, key=lambda item: item[:3])]
    accepted.extend(item[2] for item in sorted(background, key=lambda item: item[:2]))
    if top_k is not None:
        accepted = accepted[: max(top_k, 0)]
    return PassageSelection(
        accepted=tuple(accepted),
        conflicts=tuple(conflicts),
        excluded_ids=tuple(excluded),
        has_answer_evidence=any(item.metadata.get("jev_answer_evidence") == "true" for item in accepted),
        question_set_version=version,
        judged=len(candidates),
        relevant=relevant,
        with_evidence=with_evidence,
        contradicted=contradicted,
        injected=injected,
    )


def _uniform_question_set_version(judgments: Sequence[PassageJudgment]) -> str:
    """The one version that judged this batch; mixed versions are refused.

    One batch is produced by one question set, so two versions in one batch mean
    the caller merged judgments from different serving versions. Refusing is the
    only honest answer: picking either one would misattribute the response.
    """
    versions = {judgment.question_set_version for judgment in judgments}
    if len(versions) > 1:
        raise ValueError(
            f"mixed question set versions in one judgment batch: {sorted(versions)}"
        )
    return next(iter(versions), "")


def _judgments_by_chunk_id(judgments: Sequence[PassageJudgment]) -> dict[str, PassageJudgment]:
    by_chunk_id: dict[str, PassageJudgment] = {}
    for judgment in judgments:
        if judgment.chunk_id in by_chunk_id:
            raise ValueError(f"duplicate judgment for chunk {judgment.chunk_id}")
        by_chunk_id[judgment.chunk_id] = judgment
    return by_chunk_id


def _with_jev_provenance(candidate: Chunk, judgment: PassageJudgment, policy: JevPolicy) -> Chunk:
    metadata = dict(candidate.metadata)
    metadata["jev_answer_evidence"] = "true" if judgment.answer_evidence >= policy.evidence_threshold else "false"
    metadata["jev_contradiction"] = "true" if judgment.contradiction >= policy.contradiction_threshold else "false"
    metadata["jev_prompt_injection"] = "true" if judgment.prompt_injection >= policy.injection_threshold else "false"
    return replace(candidate, rerank_score=judgment.relevance, score_source="jev", metadata=metadata)


__all__ = [
    "JevPolicy",
    "KbCapabilities",
    "PassageSelection",
    "RouteDecision",
    "RouteKind",
    "choose_route",
    "select_passages",
]
