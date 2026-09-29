"""Tests for deterministic passage selection with Jev provenance."""

import pytest

from rag.jev.models import JevBatchResult, PassageJudgment
from rag.jev.policy import JevPolicy, PassageSelection, select_passages
from rag.jev.service import JevDecisionService
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
    version: str = "passages-v1",
) -> PassageJudgment:
    return PassageJudgment(
        chunk_id=chunk_id,
        relevance=relevance,
        answer_evidence=evidence,
        contradiction=contradiction,
        prompt_injection=injection,
        question_set_version=version,
    )


def test_selection_counts_what_the_judge_saw_so_a_failure_can_be_attributed():
    """判定层的原始统计量：诊断要能区分「没召回」与「召回了但没被判为证据」。

    没有这些计数时，失败只能读成"一个块都没采纳"，于是诊断一律落到内容缺口；
    有了计数就能看出候选块其实进了判定、被判为相关却没有答案证据——那是判定/改写层的问题。
    计数是**原始**量：同一块可以同时计入相关与注入（它确实同时满足两个阈值）。
    """
    selected = select_passages(
        [chunk("c0"), chunk("c1"), chunk("c2"), chunk("c3")],
        [
            judgment("c0", relevance=.9, evidence=.0, contradiction=.0, injection=.0),
            judgment("c1", relevance=.7, evidence=.8, contradiction=.0, injection=.0),
            judgment("c2", relevance=.1, evidence=.0, contradiction=.0, injection=.0),
            judgment("c3", relevance=.9, evidence=.0, contradiction=.6, injection=.9),
        ],
        policy=JevPolicy(),
    )

    assert selected.judged == 4
    assert selected.relevant == 3  # c0, c1, c3；c2 低于相关阈值
    assert selected.with_evidence == 1  # 只有 c1
    assert selected.contradicted == 1  # c3
    assert selected.injected == 1  # c3（同一块可以同时计入相关与注入）


def test_selection_carries_the_question_set_version_of_its_judgments():
    """The selection names the version that produced the judgments it applied.

    Without it the pipeline can only stamp the preflight version into
    ``decision_metadata``, so a passages candidate's traffic is unattributable.
    """
    selected = select_passages(
        [chunk("c0")],
        [judgment("c0", relevance=.9, evidence=.8, contradiction=.0, injection=.0, version="passages-v1+candidate-3")],
        policy=JevPolicy(),
    )
    assert selected.question_set_version == "passages-v1+candidate-3"


def test_mixed_question_set_versions_are_refused():
    """One batch is judged by one set; mixed versions are a bug, not a merge."""
    with pytest.raises(ValueError, match="question set"):
        select_passages(
            [chunk("c0"), chunk("c1")],
            [
                judgment("c0", relevance=.9, evidence=.8, contradiction=.0, injection=.0, version="passages-v1"),
                judgment("c1", relevance=.9, evidence=.8, contradiction=.0, injection=.0, version="passages-v1+c3"),
            ],
            policy=JevPolicy(),
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


class RecordingJevClient:
    def __init__(self):
        self.chunk_ids = None

    def judge_passages(self, state, chunk_ids):
        self.chunk_ids = list(chunk_ids)
        return JevBatchResult(
            value=tuple(
                PassageJudgment(
                    chunk_id=chunk_id,
                    relevance=0.9,
                    answer_evidence=0.9,
                    contradiction=0.0,
                    prompt_injection=0.0,
                    question_set_version="passages-v1",
                )
                for chunk_id in chunk_ids
            ),
            available=True,
            duration_ms=1.0,
        )


def test_select_caps_judged_candidates_to_policy_budget():
    client = RecordingJevClient()
    service = JevDecisionService(client=client, policy=JevPolicy(max_passage_candidates=3))
    candidates = [Chunk(text=f"chunk-{index}", doc_name="doc.md", chunk_index=index) for index in range(10)]

    selection = service.select("问题", candidates, top_k=5)

    assert client.chunk_ids == ["c0", "c1", "c2"]
    assert [item.text for item in selection.accepted] == ["chunk-0", "chunk-1", "chunk-2"]
