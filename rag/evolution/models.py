"""Immutable contracts for the assisted self-evolution observation phase.

Only hashes, scores, versions, and enums reach this package: the recorder never
persists chunk text, answers, or credentials, and :class:`DiagnosticDetail`
rejects unknown fields by construction (``extra="forbid"``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from rag.reliability.models import AnswerabilityKind, ConfidenceLevel


class EvolutionStatus(StrEnum):
    """Design section 7 state machine; arbitrary stages may exit to a terminal state."""

    OBSERVED = "observed"
    CLUSTERED = "clustered"
    AWAITING_DIAGNOSIS_REVIEW = "awaiting_diagnosis_review"
    APPROVED_FOR_CANDIDATE = "approved_for_candidate"
    CANDIDATE_TESTING = "candidate_testing"
    AWAITING_VERIFICATION = "awaiting_verification"
    APPROVED_FOR_CANARY = "approved_for_canary"
    CANARY = "canary"
    STABLE = "stable"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"
    ROLLED_BACK = "rolled_back"


class ObservationPool(StrEnum):
    """Design section 5 pools: unreproducible failures wait, three hits promote."""

    OBSERVATION_QUEUE = "observation_queue"
    CANDIDATE_POOL = "candidate_pool"


class NonEvolvableReason(StrEnum):
    """Design section 5 exclusions: permission denials and transient external faults."""

    PERMISSION_DENIED = "permission_denied"
    TRANSIENT_EXTERNAL = "transient_external"


class FailureStage(StrEnum):
    """Deterministic failure stage; the clusterer owns the authoritative value."""

    STRUCTURED_QUERY = "structured_query"
    TOOL = "tool"
    RETRIEVAL = "retrieval"
    GENERATION = "generation"
    UNKNOWN = "unknown"


class RootCauseFeature(StrEnum):
    """Deterministic root-cause subset for phase 2 (design section 6).

    The clusterer derives these from stored signals only; LLM diagnosis is the
    phase-3 ``EvolutionDiagnoser``. ``UNDETERMINED`` can never be promoted: a
    root cause that cannot be determined stays in the observation queue.
    """

    RECALL_MISS = "recall_miss"
    GENERATOR_IGNORED_EVIDENCE = "generator_ignored_evidence"
    PROMPT_OR_WORKFLOW_DEFECT = "prompt_or_workflow_defect"
    CODE_DEFECT = "code_defect"
    EVIDENCE_CONFLICT = "evidence_conflict"
    UNDETERMINED = "undetermined"


# Design section 7 transitions, single source for the read-only state-machine view.
EVOLUTION_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "observed": ("clustered", "rejected", "blocked", "needs_more_evidence"),
    "clustered": ("awaiting_diagnosis_review", "rejected", "blocked", "needs_more_evidence"),
    "awaiting_diagnosis_review": ("approved_for_candidate", "rejected", "blocked", "needs_more_evidence"),
    "approved_for_candidate": ("candidate_testing", "rejected", "blocked"),
    "candidate_testing": ("awaiting_verification", "rejected", "blocked"),
    "awaiting_verification": ("approved_for_canary", "rejected", "blocked", "needs_more_evidence"),
    "approved_for_canary": ("canary", "rejected", "blocked"),
    "canary": ("stable", "rolled_back", "rejected", "blocked", "needs_more_evidence"),
    "stable": ("rolled_back",),
    "rejected": (),
    "blocked": (),
    "needs_more_evidence": (),
    "rolled_back": (),
}


@dataclass(frozen=True)
class FailureObservation:
    """One ACL-filtered diagnosable failure trajectory, stored without raw content."""

    kb_id: str
    question: str
    route: str
    failure_stage: FailureStage = FailureStage.UNKNOWN
    root_cause_feature: str | None = None
    status: EvolutionStatus = EvolutionStatus.OBSERVED
    pool: ObservationPool = ObservationPool.OBSERVATION_QUEUE
    priority: str = "normal"
    answerability_kind: AnswerabilityKind | None = None
    confidence_level: ConfidenceLevel | None = None
    chunk_hashes: tuple[str, ...] = ()
    best_score: float | None = None
    adopted_evidence_hashes: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    structured_query_kind: str | None = None
    exception_class: str | None = None
    feedback_kind: str | None = None
    jev_question_set_version: str | None = None
    decision_metadata: dict[str, object] = field(default_factory=dict)
    total_ms: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class DiagnosticDetail(BaseModel):
    """Diagnosable signals for one request, built pipeline-side after ACL filtering.

    ``extra="forbid"`` makes an unknown field (a chunk text, an answer, a secret)
    a construction error, so sensitive content cannot reach the store silently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kb_id: str
    failure_stage: FailureStage = FailureStage.UNKNOWN
    answerability_kind: AnswerabilityKind | None = None
    confidence_level: ConfidenceLevel | None = None
    chunk_hashes: tuple[str, ...] = ()
    best_score: float | None = None
    adopted_evidence_hashes: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    structured_query_kind: str | None = None
    exception_class: str | None = None
    feedback_kind: str | None = None
    jev_question_set_version: str | None = None
    non_evolvable_reason: NonEvolvableReason | None = None


__all__ = [
    "EVOLUTION_TRANSITIONS",
    "DiagnosticDetail",
    "EvolutionStatus",
    "FailureObservation",
    "FailureStage",
    "NonEvolvableReason",
    "ObservationPool",
    "RootCauseFeature",
]
