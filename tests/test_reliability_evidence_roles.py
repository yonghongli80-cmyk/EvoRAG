"""A-03：Jev 背景相关块可以进引用，但绝不能当答案证据。

2026-10-22 审查（A-03）：``select_passages`` 把"相关但没有答案证据"的背景块与证据块一起
塞进 ``accepted``，而证据包只认 ``accepted``，于是背景块会抬置信度、甚至单独满足可答性。
这里钉住修复后的契约：

* ``EvidenceRef.role`` 区分 ``evidence`` / ``background``，默认 ``evidence``（旧调用不受影响）；
* 置信度/可答性只统计 ``role="evidence"`` 的子集；
* 背景块只进 sources：不进 generation 的上下文，也不进证据包的可答证据。
"""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from rag.jev.models import PassageJudgment
from rag.jev.policy import select_passages
from rag.models import Chunk
from rag.pipeline import RAGPipeline
from rag.reliability.answerability import decide_answerability
from rag.reliability.bundle import build_evidence_bundle
from rag.reliability.confidence import assess_confidence
from rag.reliability.models import (
    AnswerabilityDecision,
    EvidenceBundle,
    EvidenceRef,
    RetrievalMetrics,
)


def evidence_ref(*, role="evidence", score=0.8, is_current=True, document_id=7):
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


def scored_chunk(*, text="年假为 7 天", rerank_score=0.8, document_id=7):
    return Chunk(
        text=text,
        doc_name=f"doc{document_id}.md",
        chunk_index=0,
        document_id=document_id,
        document_version_id=document_id * 10,
        version_no=1,
        rerank_score=rerank_score,
        score_source="reranker" if rerank_score is not None else "unavailable",
    )


# ── EvidenceRef.role ────────────────────────────────────────────────────────


def test_evidence_ref_defaults_to_the_evidence_role():
    assert evidence_ref().role == "evidence"


def test_evidence_ref_accepts_the_background_role():
    assert evidence_ref(role="background").role == "background"


def test_evidence_ref_rejects_an_unknown_role():
    with pytest.raises(ValidationError):
        evidence_ref(role="citation")


# ── bundle builder ──────────────────────────────────────────────────────────


def test_bundle_builder_marks_background_refs_without_dropping_them():
    evidence = scored_chunk(document_id=7, rerank_score=0.8)
    background = scored_chunk(document_id=8, rerank_score=0.9)

    result = build_evidence_bundle(
        "年假是多少",
        [evidence],
        kb_id="rag_docs",
        acl_checked=True,
        background=[background],
    )

    roles = {item.document_id: item.role for item in result.current_evidence}
    assert roles == {7: "evidence", 8: "background"}
    # 指标仍描述"看过多少候选、候选里最高分是多少"，背景块也是看过的候选。
    assert result.retrieval_metrics.visible_candidates == 2
    assert result.retrieval_metrics.top_score == 0.9


def test_bundle_builder_rejects_unversioned_background_chunks():
    unversioned = Chunk(text="背景", doc_name="bg.md", chunk_index=0)

    with pytest.raises(ValueError, match="versioned evidence"):
        build_evidence_bundle(
            "年假是多少",
            [],
            kb_id="rag_docs",
            acl_checked=True,
            background=[unversioned],
        )


# ── answerability ───────────────────────────────────────────────────────────


def test_background_evidence_cannot_make_a_question_answerable():
    decision = decide_answerability(
        bundle(current=(evidence_ref(role="background", score=0.95),))
    )

    assert decision.kind == "insufficient_evidence"
    assert decision.reasons == ("no_answer_evidence",)


def test_background_history_cannot_claim_answerable_with_warning():
    decision = decide_answerability(
        bundle(historical=(evidence_ref(role="background", score=0.95, is_current=False),))
    )

    assert decision.kind == "insufficient_evidence"


def test_evidence_role_still_makes_a_question_answerable():
    decision = decide_answerability(bundle(current=(evidence_ref(score=0.8),)))

    assert decision.kind == "answerable"


# ── confidence ──────────────────────────────────────────────────────────────


def test_confidence_ignores_background_scores():
    """一个低于阈值的证据块 + 一个高分的背景块，不能凑出"单一合格来源"。"""
    evidence_bundle = bundle(
        current=(
            evidence_ref(score=0.2),
            evidence_ref(role="background", score=0.99, document_id=8),
        )
    )

    assessment = assess_confidence(
        evidence_bundle, AnswerabilityDecision(kind="answerable", reasons=())
    )

    assert assessment.level == "low"


def test_two_documents_where_one_is_background_do_not_produce_high_confidence():
    evidence_bundle = bundle(
        current=(
            evidence_ref(score=0.8, document_id=7),
            evidence_ref(role="background", score=0.8, document_id=8),
        )
    )

    assessment = assess_confidence(
        evidence_bundle, AnswerabilityDecision(kind="answerable", reasons=())
    )

    assert assessment.level == "medium"


# ── passage policy ──────────────────────────────────────────────────────────


def judgment(chunk_id: str, *, relevance: float, answer_evidence: float) -> PassageJudgment:
    return PassageJudgment(
        chunk_id=chunk_id,
        relevance=relevance,
        answer_evidence=answer_evidence,
        contradiction=0.0,
        prompt_injection=0.0,
        question_set_version="passages-v1",
    )


def test_select_passages_separates_answer_evidence_from_background():
    evidence = Chunk(text="年假为 7 天", doc_name="policy.md", chunk_index=0)
    background = Chunk(text="年假制度的背景说明", doc_name="policy.md", chunk_index=1)

    selection = select_passages(
        [evidence, background],
        [
            judgment("c0", relevance=0.9, answer_evidence=0.9),
            judgment("c1", relevance=0.8, answer_evidence=0.1),
        ],
        top_k=5,
    )

    assert [chunk.text for chunk in selection.evidence] == ["年假为 7 天"]
    assert [chunk.text for chunk in selection.background] == ["年假制度的背景说明"]
    assert [chunk.text for chunk in selection.accepted] == [
        "年假为 7 天",
        "年假制度的背景说明",
    ]
    assert selection.has_answer_evidence is True


def test_select_passages_caps_background_only_after_evidence():
    evidence = Chunk(text="证据", doc_name="policy.md", chunk_index=0)
    background = Chunk(text="背景", doc_name="policy.md", chunk_index=1)

    selection = select_passages(
        [background, evidence],
        [
            judgment("c0", relevance=0.7, answer_evidence=0.0),
            judgment("c1", relevance=0.6, answer_evidence=0.8),
        ],
        top_k=1,
    )

    assert [chunk.text for chunk in selection.accepted] == ["证据"]
    assert [chunk.text for chunk in selection.evidence] == ["证据"]
    assert selection.background == ()


# ── pipeline wiring ─────────────────────────────────────────────────────────


def _build_pipeline(jev_service, **kwargs):
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
        reranker_cls.return_value.rerank.side_effect = lambda _query, chunks: chunks
        pipeline = RAGPipeline(
            "test.txt",
            session_id="s1",
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
            jev_service=jev_service,
            **kwargs,
        )
    pipeline._feedback_processor.get_weights.return_value = {}
    return pipeline


@pytest.fixture
def active_jev(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs,kb1")


def test_background_passages_are_cited_but_never_enter_the_generation_context(active_jev):
    from rag.jev.policy import PassageSelection, RouteDecision

    evidence = Chunk(
        text="年假为 7 天", doc_name="policy.md", chunk_index=0,
        document_id=7, document_version_id=70,
        rerank_score=0.9, score_source="reranker",
    )
    background = Chunk(
        text="年假制度的历史背景", doc_name="handbook.md", chunk_index=1,
        document_id=8, document_version_id=80,
        rerank_score=0.8, score_source="reranker",
    )
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="rag", should_rewrite=False, reason="retrieval_confident", preflight=None
    )
    jev.select.return_value = PassageSelection(
        accepted=(evidence, background),
        conflicts=(),
        excluded_ids=(),
        has_answer_evidence=True,
        evidence=(evidence,),
        background=(background,),
    )
    pipeline = _build_pipeline(jev, kb_id="kb1", user_db=MagicMock())
    pipeline.retriever.retrieve.return_value = [evidence, background]

    with patch("rag.pipeline.generate", return_value="7 天") as mock_generate:
        result = pipeline.query(
            "年假有几天", user={"id": 7, "is_admin": True}, kb_id="kb1"
        )

    assert [chunk.text for chunk in result.context] == ["年假为 7 天"]
    assert [source["doc_name"] for source in result.sources] == ["policy.md", "handbook.md"]
    messages = mock_generate.call_args.args[0]
    assert "年假制度的历史背景" not in str(messages)


def test_refusal_bundle_keeps_background_refs_for_citation_only(active_jev):
    from rag.jev.policy import PassageSelection, RouteDecision

    background = Chunk(
        text="年假制度的历史背景", doc_name="handbook.md", chunk_index=1,
        document_id=8, document_version_id=80,
    )
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="rag", should_rewrite=False, reason="retrieval_confident", preflight=None
    )
    jev.select.return_value = PassageSelection(
        accepted=(background,),
        conflicts=(),
        excluded_ids=(),
        has_answer_evidence=False,
        evidence=(),
        background=(background,),
    )
    pipeline = _build_pipeline(jev, kb_id="kb1", user_db=MagicMock())
    pipeline.retriever.retrieve.return_value = [background]

    prepared, _error = pipeline._prepare_context(
        "年假有几天", "s1", None, user={"id": 7, "is_admin": True}, kb_id="kb1"
    )

    roles = [item.role for item in prepared["evidence_bundle"].current_evidence]
    assert roles == ["background"]
    assert prepared["decision"].kind == "insufficient_evidence"
    assert [source["doc_name"] for source in prepared["sources"]] == ["handbook.md"]
    assert prepared["context"] == []

    with patch("rag.pipeline.generate") as mock_generate:
        result = pipeline.query(
            "年假有几天", user={"id": 7, "is_admin": True}, kb_id="kb1"
        )

    assert result.sources and result.sources[0]["doc_name"] == "handbook.md"
    mock_generate.assert_not_called()
