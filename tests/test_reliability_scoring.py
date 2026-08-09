"""Task 1.5: retrieval-score provenance for reliability decisions."""

import math

import pytest
from pydantic import ValidationError

from rag.models import Chunk
from rag.reliability.models import EvidenceRef
from rag.reranker import Reranker
from rag.resilience import CircuitBreaker
from rag.retriever import Retriever


@pytest.fixture(autouse=True)
def isolate_reranker_circuit_breaker(monkeypatch):
    monkeypatch.setattr(
        "rag.reranker._breaker",
        CircuitBreaker(failure_threshold=5, recovery_timeout=30.0),
    )


def chunks() -> list[Chunk]:
    return [
        Chunk(text="制度 A", doc_name="a.md", chunk_index=0),
        Chunk(text="制度 B", doc_name="b.md", chunk_index=1),
    ]


def evidence_ref(*, score, score_source) -> EvidenceRef:
    return EvidenceRef(
        document_id=7,
        document_version_id=11,
        score=score,
        score_source=score_source,
        is_current=True,
        validity_status="valid",
        acl_checked=True,
    )


def test_rrf_fuse_preserves_raw_retrieval_score():
    first, second = chunks()

    result = Retriever._rrf_fuse(
        dense=[first, second],
        sparse=[first],
        top_k=2,
        rrf_k=60,
    )

    assert result[0].text == "制度 A"
    assert result[0].retrieval_score == pytest.approx(2 / 61)
    assert result[0].rerank_score is None


def test_reranker_preserves_real_relevance_score(monkeypatch):
    first, second = chunks()
    ranked = Retriever._rrf_fuse([first, second], [], top_k=2, rrf_k=60)
    monkeypatch.setattr(
        Reranker,
        "_call_rerank_api",
        lambda *_args, **_kwargs: [{"index": 1, "relevance_score": 0.82}],
    )

    result = Reranker().rerank("年假", ranked)

    assert result[0].text == "制度 B"
    assert result[0].rerank_score == 0.82
    assert result[0].retrieval_score == pytest.approx(1 / 62)


def test_reranker_fallback_does_not_fabricate_score(monkeypatch):
    ranked = Retriever._rrf_fuse(chunks(), [], top_k=2, rrf_k=60)

    def failing_reranker(*_args, **_kwargs):
        raise RuntimeError("reranker unavailable")

    monkeypatch.setattr(Reranker, "_call_rerank_api", failing_reranker)

    result = Reranker().rerank("年假", ranked)

    assert [item.rerank_score for item in result] == [None, None]
    assert all(item.retrieval_score is not None for item in result)


@pytest.mark.parametrize("invalid_score", [math.nan, math.inf, -0.01, 1.01])
def test_reranker_rejects_invalid_scores_without_fabricating_fallback(
    monkeypatch,
    invalid_score,
):
    ranked = Retriever._rrf_fuse(chunks(), [], top_k=2, rrf_k=60)
    monkeypatch.setattr(
        Reranker,
        "_call_rerank_api",
        lambda *_args, **_kwargs: [{"index": 0, "relevance_score": invalid_score}],
    )

    result = Reranker().rerank("年假", ranked)

    assert [item.rerank_score for item in result] == [None, None]


def test_evidence_ref_accepts_unavailable_score_with_explicit_source():
    evidence = evidence_ref(score=None, score_source="unavailable")

    assert evidence.score is None
    assert evidence.score_source == "unavailable"


def test_evidence_ref_rejects_inconsistent_score_source():
    with pytest.raises(ValidationError) as exc_info:
        evidence_ref(score=None, score_source="reranker")

    assert exc_info.value.errors()[0]["type"] == "value_error"
    assert "score_source must match score availability" in str(exc_info.value)
