"""Tests for deterministic passage selection with Jev provenance."""

import pytest

from rag.jev.models import PassageJudgment
from rag.jev.policy import JevPolicy, PassageSelection, select_passages
from rag.models import Chunk


def chunk(chunk_id: str) -> Chunk:
    return Chunk(text=chunk_id, doc_name="doc.md", chunk_index=0)


def judgment(
    chunk_id: str,
    *,
    relevance: float,
    evidence: float,
    contradiction: float,
    injection: float,
) -> PassageJudgment:
    return PassageJudgment(
        chunk_id=chunk_id,
        relevance=relevance,
        answer_evidence=evidence,
        contradiction=contradiction,
        prompt_injection=injection,
    )


def test_injected_answer_bearing_passage_is_excluded():
    selected = select_passages(
        [chunk("c0")],
        [judgment("c0", relevance=.99, evidence=.99, contradiction=.0, injection=.95)],
        policy=JevPolicy(injection_threshold=.7),
    )
    assert selected.accepted == ()
    assert selected.excluded_ids == ("c0",)
    assert selected.has_answer_evidence is False


def test_conflict_is_preserved_separately():
    selected = select_passages(
        [chunk("c0")],
        [judgment("c0", relevance=.9, evidence=.8, contradiction=.9, injection=.01)],
        policy=JevPolicy(),
    )
    assert selected.conflicts[0].text == "c0"
    assert selected.accepted == ()
    assert selected.has_answer_evidence is False


def test_answer_evidence_ranks_before_background():
    selected = select_passages(
        [chunk("c0"), chunk("c1")],
        [
            judgment("c0", relevance=.9, evidence=.1, contradiction=.0, injection=.0),
            judgment("c1", relevance=.4, evidence=.9, contradiction=.0, injection=.0),
        ],
        policy=JevPolicy(),
    )
    assert [item.text for item in selected.accepted] == ["c1", "c0"]
    assert selected.has_answer_evidence is True


def test_sorting_is_stable_for_equal_scores():
    selected = select_passages(
        [chunk("c0"), chunk("c1"), chunk("c2")],
        [
            judgment("c0", relevance=.8, evidence=.8, contradiction=.0, injection=.0),
            judgment("c1", relevance=.8, evidence=.8, contradiction=.0, injection=.0),
            judgment("c2", relevance=.8, evidence=.8, contradiction=.0, injection=.0),
        ],
        policy=JevPolicy(),
    )
    assert [item.text for item in selected.accepted] == ["c0", "c1", "c2"]


def test_background_only_selection_has_no_answer_evidence():
    selected = select_passages(
        [chunk("c0")],
        [judgment("c0", relevance=.9, evidence=.2, contradiction=.0, injection=.0)],
        policy=JevPolicy(),
    )
    assert [item.text for item in selected.accepted] == ["c0"]
    assert selected.has_answer_evidence is False


def test_all_no_evidence_yields_empty_selection():
    selected = select_passages(
        [chunk("c0")],
        [judgment("c0", relevance=.1, evidence=.1, contradiction=.1, injection=.0)],
        policy=JevPolicy(),
    )
    assert isinstance(selected, PassageSelection)
    assert selected.accepted == ()
    assert selected.conflicts == ()
    assert selected.has_answer_evidence is False


def test_missing_judgment_is_rejected():
    with pytest.raises(ValueError, match="missing judgment"):
        select_passages(
            [chunk("c0"), chunk("c1")],
            [judgment("c0", relevance=.9, evidence=.9, contradiction=.0, injection=.0)],
            policy=JevPolicy(),
        )


def test_top_k_caps_accepted_results():
    candidates = [chunk("c0"), chunk("c1"), chunk("c2")]
    judgments = [
        judgment("c0", relevance=.9, evidence=.9, contradiction=.0, injection=.0),
        judgment("c1", relevance=.8, evidence=.8, contradiction=.0, injection=.0),
        judgment("c2", relevance=.7, evidence=.7, contradiction=.0, injection=.0),
    ]
    selected = select_passages(candidates, judgments, policy=JevPolicy(), top_k=2)
    assert [item.text for item in selected.accepted] == ["c0", "c1"]


def test_accepted_chunks_carry_jev_score_and_source():
    selected = select_passages(
        [chunk("c0")],
        [judgment("c0", relevance=.77, evidence=.9, contradiction=.0, injection=.0)],
        policy=JevPolicy(),
    )
    accepted = selected.accepted[0]
    assert accepted.rerank_score == 0.77
    assert accepted.score_source == "jev"
    assert accepted.metadata["jev_answer_evidence"] == "true"
    assert accepted.metadata["jev_prompt_injection"] == "false"


def test_irrelevant_chunk_is_dropped_without_exclusion_record():
    selected = select_passages(
        [chunk("c0")],
        [judgment("c0", relevance=.1, evidence=.1, contradiction=.0, injection=.0)],
        policy=JevPolicy(),
    )
    assert selected.accepted == ()
    assert selected.excluded_ids == ()
