"""A-10：拒答要能区分"没检索到""有候选但没答案证据""分数不可用"。

2026-10-22 审查（A-10）：重排整体降级与"引用兜底拒答"都被记成检索缺口，且 ``best_score=None``
无法解释。修复后 ``decide_answerability`` 在 ``insufficient_evidence`` 上给出可区分的 reasons，
进化观测的 ``failure_stage`` 按 reasons 归因，重排降级（评分不可用）记成瞬时外部故障而不是可诊断
的检索缺陷。
"""

from unittest.mock import MagicMock, patch

import pytest

from rag.evolution.models import FailureStage, NonEvolvableReason
from rag.pipeline import RAGPipeline
from rag.reliability.answerability import decide_answerability
from rag.reliability.models import EvidenceBundle, EvidenceRef, RetrievalMetrics


def evidence_ref(*, score=0.8, role="evidence", is_current=True, document_id=7):
    return EvidenceRef(
        document_id=document_id,
        document_version_id=document_id * 10,
        score=score,
        score_source="reranker" if score is not None else "unavailable",
        is_current=is_current,
        validity_status="valid",
        acl_checked=True,
        role=role,
    )


def bundle(*, current=(), historical=()):
    scores = [item.score for item in (*current, *historical) if item.score is not None]
    return EvidenceBundle(
        question="年假是多少",
        current_evidence=tuple(current),
        historical_evidence=tuple(historical),
        structured_results=(),
        conflicts=(),
        retrieval_metrics=RetrievalMetrics(
            visible_candidates=len(current) + len(historical),
            top_score=max(scores) if scores else None,
            average_score=sum(scores) / len(scores) if scores else None,
        ),
    )


# ── reasons ─────────────────────────────────────────────────────────────────


def test_nothing_retrieved_is_named():
    decision = decide_answerability(bundle())

    assert decision.kind == "insufficient_evidence"
    assert decision.reasons == ("nothing_retrieved",)


def test_no_answer_evidence_is_named():
    decision = decide_answerability(bundle(current=(evidence_ref(score=0.2),)))

    assert decision.kind == "insufficient_evidence"
    assert decision.reasons == ("no_answer_evidence",)


def test_scores_unavailable_is_named():
    decision = decide_answerability(bundle(current=(evidence_ref(score=None),)))

    assert decision.kind == "insufficient_evidence"
    assert decision.reasons == ("scores_unavailable",)


def test_retrieved_background_only_is_not_reported_as_nothing_retrieved():
    """引用兜底拒答：确实召回到了候选，只是没有答案证据。"""
    decision = decide_answerability(
        bundle(current=(evidence_ref(role="background", score=0.79),))
    )

    assert decision.kind == "insufficient_evidence"
    assert decision.reasons == ("no_answer_evidence",)


def test_scored_historical_only_still_answers_with_warning():
    decision = decide_answerability(
        bundle(historical=(evidence_ref(score=0.99, is_current=False),))
    )

    assert decision.kind == "answerable_with_warning"


# ── pipeline attribution ────────────────────────────────────────────────────


def _build_pipeline():
    with patch("rag.pipeline.load", return_value="text"), \
         patch("rag.pipeline.clean_document", return_value=("text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever") as retriever_cls, \
         patch("rag.pipeline.Reranker") as reranker_cls:
        retriever_cls.return_value.retrieve.return_value = []
        reranker_cls.return_value.rerank.return_value = []
        pipeline = RAGPipeline(
            "test.txt",
            kb_id="kb1",
            user_db=MagicMock(),
            session_id="s1",
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
        )
    pipeline._feedback_processor.get_weights.return_value = {}
    return pipeline


def _prepared(pipeline, evidence_bundle):
    decision = decide_answerability(evidence_bundle)
    from rag.reliability.confidence import assess_confidence

    return {
        "decision": decision,
        "confidence": assess_confidence(evidence_bundle, decision),
        "evidence_bundle": evidence_bundle,
        "chunk_hashes": [],
        "decision_metadata": {},
    }


@pytest.mark.parametrize(
    ("evidence_bundle", "expected_stage"),
    [
        (bundle(), FailureStage.RETRIEVAL),
        (bundle(current=(evidence_ref(score=0.2),)), FailureStage.GENERATION),
        (bundle(current=(evidence_ref(score=None),)), FailureStage.UNKNOWN),
    ],
)
def test_failure_stage_follows_the_refusal_reason(evidence_bundle, expected_stage):
    pipeline = _build_pipeline()
    prepared = _prepared(pipeline, evidence_bundle)

    detail = pipeline._evolution_submission("年假是多少", prepared)

    assert detail is not None
    assert detail.failure_stage == expected_stage
    pipeline.close()


def test_scores_unavailable_is_a_transient_dependency_failure_not_a_retrieval_defect():
    """重排降级不是产品缺陷：记跳过（transient_external），不进诊断队列。"""
    pipeline = _build_pipeline()
    prepared = _prepared(pipeline, bundle(current=(evidence_ref(score=None),)))

    detail = pipeline._evolution_submission("年假是多少", prepared)

    assert detail.non_evolvable_reason == NonEvolvableReason.TRANSIENT_EXTERNAL
    pipeline.close()


def test_citation_fallback_refusal_is_not_attributed_to_retrieval_recall():
    """有相关候选、只是没有答案证据：不能记成"什么都没检索到"。"""
    pipeline = _build_pipeline()
    prepared = _prepared(
        pipeline, bundle(current=(evidence_ref(role="background", score=0.79),))
    )

    detail = pipeline._evolution_submission("年假是多少", prepared)

    assert detail.failure_stage == FailureStage.GENERATION
    assert detail.non_evolvable_reason is None
    pipeline.close()
