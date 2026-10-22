"""Immutable contracts for evidence-aware answer reliability."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ValidityStatus = Literal["valid", "reported_outdated", "confirmed_outdated"]
AuthorityLevel = Literal["authoritative", "supporting", "unknown"]
AnswerabilityKind = Literal[
    "answerable",
    "answerable_with_warning",
    "clarification_required",
    "insufficient_evidence",
    "permission_limited",
]
ConfidenceLevel = Literal["high", "medium", "low", "unavailable"]
ScoreSource = Literal["reranker", "jev", "unavailable"]
#: What a reference is allowed to prove. ``background`` refs are passages the judge found
#: relevant without answer evidence: they belong in the citation list, never in the
#: answerability/confidence arithmetic (A-03).
EvidenceRole = Literal["evidence", "background"]


class EvidenceRef(BaseModel):
    """A permission-checked reference to one retrievable evidence unit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: int
    document_version_id: int
    version_no: int | None = None
    citation_id: int | None = None
    kb_id: str = ""
    section_title: str = ""
    chunk_index: int | None = None
    score: float | None = Field(ge=0.0, le=1.0)
    score_source: ScoreSource = "reranker"
    rank: int | None = None
    is_current: bool
    validity_status: ValidityStatus
    acl_checked: bool
    authority_level: AuthorityLevel = "unknown"
    role: EvidenceRole = "evidence"
    updated_at: float | None = None

    @model_validator(mode="after")
    def require_score_provenance(self) -> EvidenceRef:
        if (self.score is None) != (self.score_source == "unavailable"):
            raise ValueError("score_source must match score availability")
        return self


class RetrievalMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    visible_candidates: int = Field(ge=0)
    top_score: float | None = None
    average_score: float | None = None
    permission_limited: bool = False


class EvidenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    current_evidence: tuple[EvidenceRef, ...]
    historical_evidence: tuple[EvidenceRef, ...]
    structured_results: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...]
    retrieval_metrics: RetrievalMetrics

    @model_validator(mode="after")
    def require_acl_checks(self) -> EvidenceBundle:
        evidence = self.current_evidence + self.historical_evidence
        if any(not item.acl_checked for item in evidence):
            raise ValueError("acl_checked evidence required")
        if any(not item.is_current for item in self.current_evidence):
            raise ValueError("current_evidence requires current evidence")
        if any(item.is_current for item in self.historical_evidence):
            raise ValueError("historical_evidence requires historical evidence")
        return self

    @property
    def current_answer_evidence(self) -> tuple[EvidenceRef, ...]:
        """Current refs that may count as answer evidence (A-03).

        Background refs are cited but never answered from: they must not raise confidence,
        satisfy answerability, or unlock the historical-only warning.
        """
        return tuple(item for item in self.current_evidence if item.role == "evidence")

    @property
    def historical_answer_evidence(self) -> tuple[EvidenceRef, ...]:
        """Historical refs that may count as answer evidence (A-03)."""
        return tuple(item for item in self.historical_evidence if item.role == "evidence")


class AnswerabilityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AnswerabilityKind
    reasons: tuple[str, ...] = ()


class ConfidenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: ConfidenceLevel
    reasons: tuple[str, ...] = ()


class ComparisonScope(BaseModel):
    """Immutable, pre-authorized document scope passed to comparison tools."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: int
    allowed_document_ids: frozenset[int]
    allowed_document_versions: frozenset[tuple[int, int]] = frozenset()
    permission_scope_fingerprint: str

    @model_validator(mode="after")
    def require_version_scope_subset(self) -> ComparisonScope:
        if any(document_id not in self.allowed_document_ids for document_id, _ in self.allowed_document_versions):
            raise ValueError("comparison version scope must be authorized")
        return self

    def version_for(self, document_id: int) -> int | None:
        for scoped_document_id, version_id in self.allowed_document_versions:
            if scoped_document_id == document_id:
                return version_id
        return None
