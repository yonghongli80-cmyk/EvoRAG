"""Internal immutable Jev contracts, independent of SDK response classes.

The pipeline only ever sees these models; SDK answer objects never leak past the adapter.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

IntentKind = Literal["structured_query", "retrieval_qa", "tool_task", "clarification"]
ComplexityKind = Literal["simple", "multi_step", "specialist"]
JevFailureKind = Literal[
    "disabled",
    "timeout",
    "rate_limited",
    "auth",
    "invalid_response",
    "service",
    # 重复采样没能在轮数内形成多数（平票）：结论本身不稳定，不能拿来当判定。
    # 它不是故障，是"这一批候选回答不了这个问题"的如实报告，因此不触发熔断。
    "unstable",
]


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
    """One candidate passage: semantic probabilities instead of a single relevance score.

    ``question_set_version`` is the versioned question set that produced this
    judgment. It mirrors :attr:`PreflightDecision.question_set_version` so a
    request judged with a candidate passage set stays attributable to that
    candidate instead of being read as the default set's traffic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    relevance: float = Field(ge=0.0, le=1.0)
    answer_evidence: float = Field(ge=0.0, le=1.0)
    contradiction: float = Field(ge=0.0, le=1.0)
    prompt_injection: float = Field(ge=0.0, le=1.0)
    question_set_version: str


class JevBatchResult[T](BaseModel):
    """A Jev call outcome: either a mapped immutable value or a normalized failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: T | None = None
    available: bool
    failure_kind: JevFailureKind | None = None
    duration_ms: float = Field(ge=0.0)
