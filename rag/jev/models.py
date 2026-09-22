"""Internal immutable Jev contracts, independent of SDK response classes.

The pipeline only ever sees these models; SDK answer objects never leak past the adapter.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

IntentKind = Literal["structured_query", "retrieval_qa", "tool_task", "clarification"]
ComplexityKind = Literal["simple", "multi_step", "specialist"]
JevFailureKind = Literal["disabled", "timeout", "rate_limited", "auth", "invalid_response", "service"]


class PreflightDecision(BaseModel):
    """One preflight batch: intent, complexity, and rewrite need with confidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: IntentKind
    intent_confidence: float = Field(ge=0.0, le=1.0)
    complexity: ComplexityKind
    complexity_confidence: float = Field(ge=0.0, le=1.0)
    rewrite_probability: float = Field(ge=0.0, le=1.0)
    model: str
    question_set_version: str
    duration_ms: float = Field(ge=0.0)


class PassageJudgment(BaseModel):
    """One candidate passage: semantic probabilities instead of a single relevance score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    relevance: float = Field(ge=0.0, le=1.0)
    answer_evidence: float = Field(ge=0.0, le=1.0)
    contradiction: float = Field(ge=0.0, le=1.0)
    prompt_injection: float = Field(ge=0.0, le=1.0)


class JevBatchResult[T](BaseModel):
    """A Jev call outcome: either a mapped immutable value or a normalized failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: T | None = None
    available: bool
    failure_kind: JevFailureKind | None = None
    duration_ms: float = Field(ge=0.0)
