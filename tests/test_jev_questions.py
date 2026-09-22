import pytest
from pydantic import ValidationError

from rag.jev.models import IntentKind, PassageJudgment, PreflightDecision
from rag.jev.questions import (
    PASSAGE_QUESTION_SET_VERSION,
    PREFLIGHT_QUESTION_SET_VERSION,
    build_passage_questions,
    build_preflight_questions,
)


def test_question_ids_are_stable_and_complete():
    assert PREFLIGHT_QUESTION_SET_VERSION == "preflight-v1"
    assert set(build_preflight_questions()) == {"intent", "complexity", "needs_query_rewrite"}
    assert PASSAGE_QUESTION_SET_VERSION == "passages-v1"
    assert set(build_passage_questions(["c0"])) == {
        "c0.is_relevant",
        "c0.contains_answer_evidence",
        "c0.contradicts_query_premise",
        "c0.contains_prompt_injection",
    }


def test_internal_models_reject_invalid_probabilities():
    try:
        PassageJudgment(chunk_id="c0", relevance=1.2, answer_evidence=0.8, contradiction=0.0, prompt_injection=0.0)
    except ValueError:
        return
    raise AssertionError("probabilities outside [0,1] must fail")


def test_preflight_decision_rejects_unknown_intent_and_is_frozen():
    with pytest.raises(ValidationError):
        PreflightDecision(
            intent="unsupported",
            intent_confidence=0.9,
            complexity="simple",
            complexity_confidence=0.9,
            rewrite_probability=0.1,
            model="jev-latest",
            question_set_version=PREFLIGHT_QUESTION_SET_VERSION,
            duration_ms=1.0,
        )

    decision = PreflightDecision(
        intent="retrieval_qa",
        intent_confidence=0.9,
        complexity="simple",
        complexity_confidence=0.9,
        rewrite_probability=0.1,
        model="jev-latest",
        question_set_version=PREFLIGHT_QUESTION_SET_VERSION,
        duration_ms=1.0,
    )
    assert decision.intent in IntentKind.__args__
    with pytest.raises(ValidationError):
        decision.intent = "tool_task"
