"""Task 1: immutable, permission-safe reliability contracts."""

import pytest
from pydantic import ValidationError

from config import Settings
from rag.reliability.models import (
    AnswerabilityDecision,
    ComparisonScope,
    ConfidenceAssessment,
    EvidenceBundle,
    EvidenceRef,
    RetrievalMetrics,
)


def test_reliability_settings_have_safe_defaults():
    isolated_settings = Settings(_env_file=None)

    assert isolated_settings.reliability_min_score == 0.35
    assert isolated_settings.comparison_max_documents == 5


def evidence_ref(*, acl_checked: bool = True, is_current: bool = True, score_source: str = "reranker") -> EvidenceRef:
    return EvidenceRef(
        document_id=7,
        document_version_id=11,
        version_no=3,
        citation_id=19,
        kb_id="rag_docs",
        section_title="休假制度",
        chunk_index=2,
        score=0.8,
        score_source=score_source,
        rank=1,
        is_current=is_current,
        validity_status="valid",
        acl_checked=acl_checked,
        authority_level="authoritative",
        updated_at=1_786_176_000.0,
    )


def test_evidence_ref_accepts_explicit_jev_score_source():
    assert evidence_ref(score_source="jev").score_source == "jev"


def test_evidence_ref_rejects_jev_source_without_score():
    with pytest.raises(ValueError, match="score_source must match score availability"):
        EvidenceRef(
            document_id=7,
            document_version_id=11,
            score=None,
            score_source="jev",
            is_current=True,
            validity_status="valid",
            acl_checked=True,
        )


@pytest.mark.parametrize("bucket", ["current_evidence", "historical_evidence"])
def test_evidence_bundle_rejects_acl_unchecked_evidence(bucket):
    values = {
        "current_evidence": (),
        "historical_evidence": (),
    }
    values[bucket] = (evidence_ref(acl_checked=False, is_current=bucket == "current_evidence"),)

    with pytest.raises(ValueError, match="acl_checked"):
        EvidenceBundle(
            question="年假是多少",
            **values,
            structured_results=(),
            conflicts=(),
            retrieval_metrics=RetrievalMetrics(visible_candidates=1, top_score=0.8),
        )


@pytest.mark.parametrize(
    ("current_evidence", "historical_evidence", "message"),
    [
        ((evidence_ref(is_current=False),), (), "current_evidence requires current evidence"),
        ((), (evidence_ref(is_current=True),), "historical_evidence requires historical evidence"),
    ],
)
def test_evidence_bundle_rejects_evidence_in_the_wrong_version_bucket(
    current_evidence,
    historical_evidence,
    message,
):
    with pytest.raises(ValueError, match=message):
        EvidenceBundle(
            question="年假是多少",
            current_evidence=current_evidence,
            historical_evidence=historical_evidence,
            structured_results=(),
            conflicts=(),
            retrieval_metrics=RetrievalMetrics(visible_candidates=1, top_score=0.8),
        )


def test_evidence_ref_preserves_version_and_citation_identity():
    evidence = evidence_ref()

    assert evidence.document_id == 7
    assert evidence.document_version_id == 11
    assert evidence.version_no == 3
    assert evidence.citation_id == 19
    assert evidence.section_title == "休假制度"
    assert evidence.chunk_index == 2

    with pytest.raises(ValidationError, match="frozen"):
        evidence.score = 0.1


def test_reliability_decisions_and_comparison_scope_are_frozen():
    decision = AnswerabilityDecision(kind="answerable", reasons=("current evidence available",))
    assessment = ConfidenceAssessment(level="high", reasons=("authoritative current evidence",))
    scope = ComparisonScope(
        actor_id=3,
        allowed_document_ids=frozenset({7, 8}),
        permission_scope_fingerprint="scope-v1",
    )

    assert decision.kind == "answerable"
    assert assessment.level == "high"
    assert scope.allowed_document_ids == frozenset({7, 8})

    with pytest.raises(ValidationError, match="frozen"):
        scope.actor_id = 4
