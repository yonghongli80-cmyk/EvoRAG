"""Deterministic answerability decisions and fixed refusal copy."""

from config import settings
from rag.reliability.models import AnswerabilityDecision, AnswerabilityKind, EvidenceBundle

_FIXED_DECISION_COPY = {
    "clarification_required": "现有可见资料存在冲突或范围不明确，请补充适用时间、部门或文件范围。",
    "insufficient_evidence": "现有可见资料不足，无法确认答案。",
    "permission_limited": "当前可访问的资料不足，无法确认答案。",
}


def _insufficient_reasons(bundle: EvidenceBundle) -> tuple[str, ...]:
    """Why no answer evidence qualified — three distinct situations (A-10).

    * ``nothing_retrieved``: the retrieval set was empty; a recall/content gap.
    * ``scores_unavailable``: passages were retrieved but carry no score provenance,
      i.e. the scoring/rerank dependency degraded — not a retrieval defect.
    * ``no_answer_evidence``: passages were retrieved and scored, but none carries
      answer evidence (including the citation-only fallback refusal).
    """
    retrieved = bundle.current_evidence + bundle.historical_evidence
    if not retrieved:
        return ("nothing_retrieved",)
    if not any(item.score is not None for item in retrieved):
        return ("scores_unavailable",)
    return ("no_answer_evidence",)


def decide_answerability(bundle: EvidenceBundle) -> AnswerabilityDecision:
    current_evidence = bundle.current_answer_evidence
    if bundle.retrieval_metrics.permission_limited and not current_evidence:
        return AnswerabilityDecision(
            kind="permission_limited",
            reasons=("visible evidence is insufficient",),
        )
    if bundle.conflicts:
        return AnswerabilityDecision(
            kind="clarification_required",
            reasons=("unresolved evidence conflict",),
        )
    qualified = [
        item
        for item in current_evidence
        if item.score is not None and item.score >= settings.reliability_min_score
    ]
    if qualified:
        return AnswerabilityDecision(
            kind="answerable",
            reasons=("current evidence available",),
        )
    if bundle.historical_answer_evidence:
        return AnswerabilityDecision(
            kind="answerable_with_warning",
            reasons=("historical evidence only",),
        )
    return AnswerabilityDecision(
        kind="insufficient_evidence",
        reasons=_insufficient_reasons(bundle),
    )


def fixed_decision_copy(kind: AnswerabilityKind) -> str:
    try:
        return _FIXED_DECISION_COPY[kind]
    except KeyError as exc:
        raise ValueError("fixed copy is available only for non-generation decisions") from exc
