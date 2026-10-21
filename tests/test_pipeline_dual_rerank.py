"""双路重排：改写句低分时用原问题再排一次，按块取较高分。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from config import settings
from rag.models import Chunk
from rag.pipeline import RAGPipeline


class FakeReranker:
    """Return a fixed ranking per query; record the calls, two positional args only."""

    def __init__(self, rankings: dict[str, list[Chunk]]):
        self.rankings = rankings
        self.calls: list[tuple[str, list[Chunk]]] = []

    def rerank(self, query, documents):
        self.calls.append((query, documents))
        return self.rankings.get(query, [])


def _chunk(index: int, score: float | None, name: str = "doc") -> Chunk:
    return Chunk(
        text=f"chunk-{index}",
        doc_name=name,
        chunk_index=index,
        rerank_score=score,
        score_source="reranker" if score is not None else "unavailable",
    )


def _pipeline(reranker: FakeReranker) -> RAGPipeline:
    pipeline = object.__new__(RAGPipeline)
    pipeline.reranker = reranker
    return pipeline


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "rewrite_dual_rerank_mode", "below_threshold", raising=False)
    monkeypatch.setattr(settings, "rewrite_dual_rerank_threshold", 0.5, raising=False)


def test_low_rewrite_score_triggers_the_original_question():
    candidate = _chunk(0, None)
    low = replace(candidate, rerank_score=0.05, score_source="reranker")
    high = replace(candidate, rerank_score=0.80, score_source="reranker")
    reranker = FakeReranker({"改写问题": [low], "原问题": [high]})
    pipeline = _pipeline(reranker)
    record: dict = {}

    result = pipeline._rerank_for_evidence("原问题", "改写问题", [candidate], record=record)

    assert [call[0] for call in reranker.calls] == ["改写问题", "原问题"]
    assert result[0].rerank_score == 0.80
    assert record["rewrite_dual_rerank"] is True


def test_high_rewrite_score_keeps_a_single_pass():
    high = _chunk(0, 0.80)
    reranker = FakeReranker({"改写问题": [high]})
    pipeline = _pipeline(reranker)
    record: dict = {}

    result = pipeline._rerank_for_evidence("原问题", "改写问题", [high], record=record)

    assert len(reranker.calls) == 1
    assert result[0].rerank_score == 0.80
    assert record["rewrite_dual_rerank"] is False


def test_identical_queries_keep_a_single_pass():
    scored = _chunk(0, 0.10)
    reranker = FakeReranker({"原问题": [scored]})
    pipeline = _pipeline(reranker)

    pipeline._rerank_for_evidence("原问题", "原问题 ", [scored])

    assert len(reranker.calls) == 1


def test_degraded_reranker_does_not_get_a_second_call():
    unscored = [_chunk(0, None), _chunk(1, None)]
    reranker = FakeReranker({"改写问题": unscored})
    pipeline = _pipeline(reranker)

    result = pipeline._rerank_for_evidence("原问题", "改写问题", unscored)

    assert len(reranker.calls) == 1
    assert [chunk.rerank_score for chunk in result] == [None, None]


def test_mode_off_keeps_the_old_behaviour(monkeypatch):
    low = _chunk(0, 0.05)
    high = _chunk(0, 0.80)
    monkeypatch.setattr(settings, "rewrite_dual_rerank_mode", "off", raising=False)
    reranker = FakeReranker({"改写问题": [low], "原问题": [high]})
    pipeline = _pipeline(reranker)

    result = pipeline._rerank_for_evidence("原问题", "改写问题", [low])

    assert len(reranker.calls) == 1
    assert result[0].rerank_score == 0.05


def test_always_mode_runs_even_on_a_high_score(monkeypatch):
    high = _chunk(0, 0.90)
    monkeypatch.setattr(settings, "rewrite_dual_rerank_mode", "always", raising=False)
    reranker = FakeReranker({"改写问题": [high], "原问题": [high]})
    pipeline = _pipeline(reranker)

    pipeline._rerank_for_evidence("原问题", "改写问题", [high])

    assert len(reranker.calls) == 2


def test_merge_keeps_the_higher_score_per_chunk_and_dedupes():
    first = _chunk(0, 0.05)
    second = _chunk(1, 0.30)
    first_high = replace(first, rerank_score=0.80, score_source="reranker")
    reranker = FakeReranker(
        {"改写问题": [first, second], "原问题": [first_high]}
    )
    pipeline = _pipeline(reranker)

    result = pipeline._rerank_for_evidence("原问题", "改写问题", [first, second])

    assert len(result) == 2
    assert [chunk.chunk_index for chunk in result] == [0, 1]
    assert result[0].rerank_score == 0.80
    assert result[1].rerank_score == 0.30
    assert result[0].text == first.text


def test_different_chunks_are_unioned():
    left = _chunk(0, 0.40, name="a")
    right = _chunk(1, 0.70, name="b")
    reranker = FakeReranker({"改写问题": [left], "原问题": [right]})
    pipeline = _pipeline(reranker)

    result = pipeline._rerank_for_evidence("原问题", "改写问题", [left, right])

    assert {chunk.doc_name for chunk in result} == {"a", "b"}
    assert [chunk.rerank_score for chunk in result] == [0.70, 0.40]


def test_no_candidates_short_circuits():
    reranker = FakeReranker({})
    pipeline = _pipeline(reranker)

    assert pipeline._rerank_for_evidence("原问题", "改写问题", []) == []
    assert reranker.calls == []
