"""Deterministic route and rewrite policy.

All thresholds live in :class:`JevPolicy`, calibrated against project data rather than copied
from documentation. Jev never encodes route policy in prompt prose; this module owns routing.
"""

from dataclasses import dataclass
from typing import Literal

from rag.jev.models import PreflightDecision

RouteKind = Literal["structured", "rag", "tool", "clarification", "legacy"]


@dataclass(frozen=True)
class KbCapabilities:
    """Permission-safe capability summary shared with the preflight state."""

    has_structured_data: bool = False
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class JevPolicy:
    """Confidence and rewrite thresholds used by the deterministic policy."""

    route_confidence: float = 0.75
    rewrite_probability: float = 0.5


@dataclass(frozen=True)
class RouteDecision:
    """The deterministic route choice derived from one preflight decision."""

    route: RouteKind
    should_rewrite: bool
    reason: str
    preflight: PreflightDecision | None = None


def choose_route(
    decision: PreflightDecision,
    *,
    capabilities: KbCapabilities | None = None,
    policy: JevPolicy | None = None,
    threshold: float | None = None,
) -> RouteDecision:
    """Route deterministically from a preflight decision.

    Low confidence never selects a high-risk route: ``tool_task`` and uncertain structured or
    retrieval intents fall back to the legacy router, while uncertain clarification defaults to
    RAG. ``capabilities=None`` means the structured capability was not evaluated.
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
        return RouteDecision("legacy", should_rewrite, "retrieval_low_confidence", decision)
    return RouteDecision("rag", should_rewrite, "retrieval_confident", decision)


__all__ = ["JevPolicy", "KbCapabilities", "RouteDecision", "RouteKind", "choose_route"]
