"""Task 2: deterministic answerability decisions before generation."""

import pytest

from rag.models import Chunk
from rag.reliability.answerability import decide_answerability, fixed_decision_copy
from rag.reliability.bundle import build_evidence_bundle
from rag.reliability.models import EvidenceBundle, EvidenceRef, RetrievalMetrics


def evidence(*, score=0.8, is_current=True) -> EvidenceRef:
    return EvidenceRef(
        document_id=7,
        document_version_id=11,
        score=score,
        score_source="reranker" if score is not None else "unavailable",
        is_current=is_current,
        validity_status="valid",
        acl_checked=True,
    )


def bundle(
    *,
    current=(),
    historical=(),
    conflicts=(),
    permission_limited=False,
) -> EvidenceBundle:
    scores = [item.score for item in (*current, *historical) if item.score is not None]
    return EvidenceBundle(
        question="年假是多少",
        current_evidence=tuple(current),
        historical_evidence=tuple(historical),
        structured_results=(),
        conflicts=tuple(conflicts),
        retrieval_metrics=RetrievalMetrics(
            visible_candidates=len(current) + len(historical),
            top_score=max(scores) if scores else None,
            average_score=sum(scores) / len(scores) if scores else None,
            permission_limited=permission_limited,
        ),
    )


@pytest.mark.parametrize(
    ("evidence_bundle", "expected_kind"),
    [
        (bundle(), "insufficient_evidence"),
        (bundle(current=(evidence(score=0.2),)), "insufficient_evidence"),
        (bundle(current=(evidence(score=None),)), "insufficient_evidence"),
        (
            bundle(historical=(evidence(score=0.99, is_current=False),)),
            "answerable_with_warning",
        ),
        (bundle(current=(evidence(score=0.8),)), "answerable"),
        (
            bundle(current=(evidence(score=0.8),), conflicts=({"id": 1},)),
            "clarification_required",
        ),
        (bundle(permission_limited=True), "permission_limited"),
    ],
)
def test_answerability_uses_ordered_deterministic_rules(evidence_bundle, expected_kind):
    assert decide_answerability(evidence_bundle).kind == expected_kind


@pytest.mark.parametrize(
    "kind",
    ["clarification_required", "insufficient_evidence", "permission_limited"],
)
def test_fixed_decision_copy_is_safe_and_contains_no_percentage(kind):
    copy = fixed_decision_copy(kind)

    assert copy.strip()
    assert "%" not in copy
    assert "％" not in copy


@pytest.mark.parametrize("kind", ["answerable", "answerable_with_warning"])
def test_fixed_decision_copy_rejects_generation_decisions(kind):
    with pytest.raises(ValueError, match="fixed copy"):
        fixed_decision_copy(kind)


def scored_chunk(
    *,
    document_id=7,
    document_version_id=11,
    is_current=True,
    historical_weight=1.0,
    rerank_score=0.8,
) -> Chunk:
    return Chunk(
        text="年假为 7 天",
        doc_name="policy.pdf",
        chunk_index=2,
        document_id=document_id,
        document_version_id=document_version_id,
        version_no=3,
        is_current=is_current,
        validity_status="valid",
        section_title="休假制度",
        updated_at=1_786_176_000.0,
        historical_weight=historical_weight,
        retrieval_score=0.03,
        rerank_score=rerank_score,
    )


def test_bundle_builder_requires_explicit_acl_boundary():
    with pytest.raises(ValueError, match="ACL-filtered"):
        build_evidence_bundle(
            "年假是多少",
            [scored_chunk()],
            kb_id="rag_docs",
            acl_checked=False,
        )


def test_bundle_builder_uses_sqlite_selected_history_state_not_qdrant_flag():
    current_with_stale_flag = scored_chunk(is_current=False)
    history_with_stale_flag = scored_chunk(
        document_version_id=10,
        is_current=True,
        historical_weight=0.35,
        rerank_score=0.99,
    )

    result = build_evidence_bundle(
        "年假是多少",
        [current_with_stale_flag, history_with_stale_flag],
        kb_id="rag_docs",
        acl_checked=True,
    )

    assert [item.document_version_id for item in result.current_evidence] == [11]
    assert [item.document_version_id for item in result.historical_evidence] == [10]
    assert result.current_evidence[0].score_source == "reranker"
    assert result.retrieval_metrics.visible_candidates == 2
    assert result.retrieval_metrics.top_score == 0.8
    assert result.retrieval_metrics.average_score == 0.8


def test_bundle_builder_marks_missing_rerank_score_unavailable():
    result = build_evidence_bundle(
        "年假是多少",
        [scored_chunk(rerank_score=None)],
        kb_id="rag_docs",
        acl_checked=True,
    )

    assert result.current_evidence[0].score is None
    assert result.current_evidence[0].score_source == "unavailable"
    assert result.retrieval_metrics.top_score is None


@pytest.mark.parametrize(
    ("document_id", "document_version_id"),
    [(None, 11), (7, None)],
)
def test_bundle_builder_rejects_unversioned_enterprise_evidence(
    document_id,
    document_version_id,
):
    with pytest.raises(ValueError, match="versioned evidence"):
        build_evidence_bundle(
            "年假是多少",
            [
                scored_chunk(
                    document_id=document_id,
                    document_version_id=document_version_id,
                )
            ],
            kb_id="rag_docs",
            acl_checked=True,
        )
