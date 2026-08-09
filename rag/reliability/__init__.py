"""Public answer-reliability contracts."""

from rag.reliability.answerability import decide_answerability, fixed_decision_copy
from rag.reliability.bundle import build_evidence_bundle
from rag.reliability.models import (
    AnswerabilityDecision,
    ComparisonScope,
    ConfidenceAssessment,
    EvidenceBundle,
    EvidenceRef,
    RetrievalMetrics,
)

__all__ = [
    "AnswerabilityDecision",
    "ComparisonScope",
    "ConfidenceAssessment",
    "EvidenceBundle",
    "EvidenceRef",
    "RetrievalMetrics",
    "decide_answerability",
    "fixed_decision_copy",
    "build_evidence_bundle",
]
