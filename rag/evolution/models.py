"""Immutable contracts for the assisted self-evolution observation phase.

Only hashes, scores, versions, and enums reach this package: the recorder never
persists chunk text, answers, or credentials, and :class:`DiagnosticDetail`
rejects unknown fields by construction (``extra="forbid"``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

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


@dataclass(frozen=True)
class ReproductionResult:
    """Deterministic reproduction check: does the failure still happen the same way?"""

    reproducible: bool
    missing_hashes: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class EvidencePack:
    """Deterministic-first evidence for one candidate cluster.

    Priority order follows design section 6: admin-confirmed labels and deterministic
    evidence first, same-KB success traces second, re-retrieved permission-filtered
    candidates third. The pack never carries credentials or unauthorized text.

    ``question_term_coverage`` is deterministic like ``reproduction``: per question
    term, how many re-retrieved chunks contain it and the earliest rank one does.
    It answers "is the question's own wording anywhere in the recall set (and how
    deep)" without a model, which is what separates "the content exists but was not
    adopted" from "the recall set does not contain it".
    """

    cluster_id: int
    kb_id: str
    question_samples: tuple[str, ...]
    failure_stage: str
    root_cause_feature: str
    admin_labels: tuple[dict, ...] = ()
    success_traces: tuple[dict, ...] = ()
    recall_hashes: tuple[str, ...] = ()
    failed_hashes: tuple[str, ...] = ()
    recall_previews: tuple[dict, ...] = ()
    question_term_coverage: tuple[dict, ...] = ()
    #: 失败请求里**判定层自己报的**原始统计量：候选数（passage_judged），以及判为相关 /
    #: 含答案证据 / 前提冲突 / 注入的块数。空元组表示该失败早于统计量上线（读不到，不等于 0）。
    #: 它把"一个块都没采纳"拆成两种完全不同的失败：候选没进来（召回/内容缺口），
    #: 还是候选进来了、被判为相关却没有一块含答案证据（判定/改写层）。
    passage_judging: tuple[tuple[str, int], ...] = ()
    reproduction: ReproductionResult = ReproductionResult(reproducible=False)


class DiagnosisDraft(BaseModel):
    """One structured diagnosis draft; a draft is never a confirmed conclusion.

    The nine fields mirror design section 6; ``confidence`` is ``low`` unless admin
    labels or deterministic evidence back the root cause.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_category: Literal[
        "intent_route_error",
        "rewrite_information_loss",
        "recall_miss",
        "ranking_error",
        "chunking_structure_damage",
        "tool_selection_error",
        "structured_plan_error",
        "generator_ignored_evidence",
        "evidence_conflict_unrecognized",
        "prompt_or_workflow_defect",
        "code_defect",
        "undetermined",
    ]
    stage: str
    first_divergence: str
    direct_evidence: str
    root_cause: str
    impact_scope: str
    suggested_layer: Literal[
        "prompt",
        "retrieval_config",
        "query_rewrite",
        "field_mapping",
        "chunking",
        "reranking",
        "parser",
        "tool_workflow",
        "code",
        "none",
    ]
    reproduction_steps: str
    acceptance_criteria: str
    confidence: Literal["low", "medium", "high"]
    reproducible: bool
    evidence_refs: tuple[str, ...] = ()


__all__ = [
    "EVOLUTION_TRANSITIONS",
    "DiagnosisDraft",
    "DiagnosticDetail",
    "EvidencePack",
    "EvolutionStatus",
    "FailureObservation",
    "FailureStage",
    "NonEvolvableReason",
    "ObservationPool",
    "RootCauseFeature",
]
