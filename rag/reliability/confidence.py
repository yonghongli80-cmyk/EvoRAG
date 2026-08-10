"""Deterministic, explainable confidence assessment."""

from config import settings
from rag.reliability.models import (
    AnswerabilityDecision,
    ConfidenceAssessment,
    EvidenceBundle,
)


def assess_confidence(
    bundle: EvidenceBundle,
    decision: AnswerabilityDecision,
) -> ConfidenceAssessment:
    """Assess confidence from visible evidence without fabricating a score."""
    if decision.kind in {
        "clarification_required",
        "insufficient_evidence",
        "permission_limited",
    }:
        return ConfidenceAssessment(
            level="unavailable",
            reasons=("answer was not generated",),
        )

    if decision.kind == "answerable_with_warning":
        return ConfidenceAssessment(
            level="low",
            reasons=("historical evidence only",),
        )

    qualified = tuple(
        item
        for item in bundle.current_evidence
        if item.validity_status == "valid"
        and item.score is not None
        and item.score >= settings.reliability_min_score
    )
    if any(item.authority_level == "authoritative" for item in qualified):
        return ConfidenceAssessment(
            level="high",
            reasons=("authoritative current source",),
        )

    source_count = len({item.document_id for item in qualified})
    if source_count >= 2 and not bundle.conflicts:
        return ConfidenceAssessment(
            level="high",
            reasons=("multiple consistent current sources",),
        )
    if qualified:
        return ConfidenceAssessment(
            level="medium",
            reasons=("single qualifying current source",),
        )
    return ConfidenceAssessment(
        level="low",
        reasons=("current evidence coverage is limited",),
    )
