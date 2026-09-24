"""Task 1: candidate contracts and the question-set payload boundary.

Only one artifact type exists in this phase: ``jev_question_set`` — a text
variant of the Jev judgement layer's question set. Its payload may only
rewrite question *text* (``instructions`` and ``criteria`` values).

Everything else is structural and must stay untouched, because the deterministic
mappers (``_map_preflight``/``_map_passages``) and the stored schema depend on
it: question ids, question count, primitive type (``Choice``/``Noul``) and the
criteria key set. These tests pin that boundary plus the two text bounds
(per-field length and the ≤40% change magnitude per field).
"""

import pytest
from pydantic import ValidationError
from typesafe_sdk import Choice, Noul

from rag.evolution.candidates import (
    JevQuestionSetCandidate,
    PayloadBoundaryError,
    validate_payload,
)
from rag.jev.questions import build_passage_questions, build_preflight_questions


def preflight_base() -> dict[str, object]:
    """The current preflight question set, exactly as ``rag.jev`` serves it."""
    return build_preflight_questions()


def passages_base() -> dict[str, object]:
    """A two-chunk passage question set (four questions per chunk)."""
    return build_passage_questions(["c1", "c2"])


def full_payload(base: dict[str, object], **overrides: dict) -> dict[str, dict]:
    """A payload that echoes every base id; ``overrides`` edits selected entries.

    An entry without overrides means "unchanged", so a candidate can change one
    question without rewriting the whole set.
    """
    payload: dict[str, dict] = {}
    for question_id, override in overrides.items():
        payload[question_id] = dict(override)
    for question_id in base:
        payload.setdefault(question_id, {})
    return payload


def sample_candidate(**kw) -> JevQuestionSetCandidate:
    """A minimal valid ``JevQuestionSetCandidate`` (full payload over preflight)."""
    base = preflight_base()
    data = dict(
        artifact_type="jev_question_set",
        target="preflight",
        payload=full_payload(
            base,
            intent={"instructions": "判断用户问题的主要意图；只依据问题本身和提供的知识库能力摘要。"},
        ),
        rationale="intent 判据在结构化查询边界与线上分歧集中",
        expected_effect="结构化查询意图判定更稳定",
        rollback_ref={"question_set_version": "preflight-v1", "payload_sha256": "a" * 64},
        confidence="medium",
    )
    data.update(kw)
    return JevQuestionSetCandidate(**data)


# ------------------------------------------------------------------
# Text-only rewrites pass the boundary.
# ------------------------------------------------------------------


def test_equivalent_text_rewrite_of_instructions_is_accepted():
    base = preflight_base()
    payload = full_payload(
        base,
        intent={
            "instructions": "判断用户问题的主要意图；只依据问题本身和提供的知识库能力摘要，"
            "不得推测未提供的信息（结构化查询边界除外）。"
        },
    )
    assert validate_payload("preflight", payload, base) is None


def test_criteria_only_override_of_a_passage_question_is_accepted():
    base = passages_base()
    payload = full_payload(
        base,
        **{
            "c1.is_relevant": {
                "criteria": {
                    "true": "文本与问题主题直接相关。",
                    "false": "文本与問題主题无关。",
                }
            }
        },
    )
    assert validate_payload("passages", payload, base) is None


def test_unchanged_entries_are_allowed_next_to_one_edited_question():
    base = preflight_base()
    payload = full_payload(
        base, complexity={"instructions": "判断回答该问题所需的步骤数量与专业深度（含工具调用）。"}
    )
    assert validate_payload("preflight", payload, base) is None


def test_dict_shaped_base_questions_are_supported():
    """A question set may be plain ``{"type": ..., "instructions": ...}`` dicts."""
    base = {"intent": {"type": "choice", "instructions": "a" * 100, "criteria": {"x": "b" * 10}}}
    payload = {"intent": {"instructions": "a" * 60 + "b" * 40}}
    assert validate_payload("preflight", payload, base) is None


def test_validation_never_mutates_the_base_question_set():
    base = preflight_base()
    before = {qid: question.model_dump() for qid, question in base.items()}
    payload = full_payload(
        base,
        intent={
            "instructions": "判断用户问题的主要意图；只依据问题本身和提供的知识库能力摘要，"
            "不得推测未提供的信息（结构化查询边界除外）。"
        },
    )
    validate_payload("preflight", payload, base)
    assert {qid: question.model_dump() for qid, question in base.items()} == before


# ------------------------------------------------------------------
# Structure: ids, primitive type, criteria keys.
# ------------------------------------------------------------------


def test_empty_payload_is_rejected():
    with pytest.raises(PayloadBoundaryError, match="empty"):
        validate_payload("preflight", {}, preflight_base())


def test_payload_that_changes_no_question_text_is_rejected():
    base = preflight_base()
    with pytest.raises(PayloadBoundaryError, match="no question text"):
        validate_payload("preflight", full_payload(base), base)


def test_new_question_id_is_rejected():
    base = preflight_base()
    payload = full_payload(base, intent={"instructions": "判断用户问题的主要意图。"})
    payload["intent_v2"] = {"instructions": "新增问题"}
    with pytest.raises(PayloadBoundaryError, match="unknown question id"):
        validate_payload("preflight", payload, base)


def test_deleted_question_id_is_rejected():
    base = preflight_base()
    payload = full_payload(base, intent={"instructions": "判断用户问题的主要意图。"})
    del payload["complexity"]
    with pytest.raises(PayloadBoundaryError, match="missing question id"):
        validate_payload("preflight", payload, base)


def test_new_passage_question_id_is_rejected():
    base = passages_base()
    payload = full_payload(base, **{"c1.is_relevant": {"instructions": "判断 c1 是否相关。"}})
    payload["c3.is_relevant"] = {"instructions": "新增块"}
    with pytest.raises(PayloadBoundaryError, match="unknown question id"):
        validate_payload("passages", payload, base)


def test_entry_keys_outside_the_text_boundary_are_rejected():
    base = preflight_base()
    payload = full_payload(base, intent={"instructions": "判断用户问题的主要意图。", "type": "noul"})
    with pytest.raises(PayloadBoundaryError, match="only"):
        validate_payload("preflight", payload, base)


def test_primitive_type_cannot_be_smuggled_in_through_an_unknown_key():
    """``Choice``→``Noul`` needs a type change; the payload has no place for it."""
    base = preflight_base()
    payload = full_payload(base, intent={"criteria": {}, "primitive": "noul"})
    with pytest.raises(PayloadBoundaryError, match="only"):
        validate_payload("preflight", payload, base)
    assert base["intent"].__class__.__name__ == "Choice"


def test_added_criteria_key_is_rejected():
    base = preflight_base()
    payload = full_payload(
        base,
        intent={
            "criteria": {
                "structured_query": "按维度汇总或计算结构化数据。",
                "retrieval_qa": "可在授权知识库中检索并回答。",
                "tool_task": "需要受控工具或多步推理。",
                "clarification": "问题含糊，需要先澄清。",
                "maybe": "新增判定分支",
            }
        },
    )
    with pytest.raises(PayloadBoundaryError, match="criteria keys"):
        validate_payload("preflight", payload, base)


def test_removed_criteria_key_is_rejected():
    base = preflight_base()
    payload = full_payload(
        base,
        intent={
            "criteria": {
                "structured_query": "按维度汇总或计算结构化数据。",
                "retrieval_qa": "可在授权知识库中检索并回答。",
                "tool_task": "需要受控工具或多步推理。",
            }
        },
    )
    with pytest.raises(PayloadBoundaryError, match="criteria keys"):
        validate_payload("preflight", payload, base)


def test_renamed_criteria_key_is_rejected():
    base = preflight_base()
    payload = full_payload(
        base,
        intent={
            "criteria": {
                "structured_query": "按维度汇总或计算结构化数据。",
                "retrieval": "可在授权知识库中检索并回答。",
                "tool_task": "需要受控工具或多步推理。",
                "clarification": "问题含糊，需要先澄清。",
            }
        },
    )
    with pytest.raises(PayloadBoundaryError, match="criteria keys"):
        validate_payload("preflight", payload, base)


def test_non_string_override_values_are_rejected():
    base = preflight_base()
    payload = full_payload(base, intent={"instructions": 42})
    with pytest.raises(PayloadBoundaryError, match="must be a string"):
        validate_payload("preflight", payload, base)

    # The full base key set keeps the key check quiet so the value type is what fails.
    criteria_payload = full_payload(
        base,
        intent={
            "criteria": {
                "structured_query": 1,
                "retrieval_qa": "可在授权知识库中检索并回答。",
                "tool_task": "需要受控工具或多步推理。",
                "clarification": "问题含糊，需要先澄清。",
            }
        },
    )
    with pytest.raises(PayloadBoundaryError, match="must be a string"):
        validate_payload("preflight", criteria_payload, base)


def test_empty_override_text_is_rejected():
    base = preflight_base()
    with pytest.raises(PayloadBoundaryError, match="empty"):
        validate_payload("preflight", full_payload(base, intent={"instructions": "   "}), base)


def test_unknown_target_is_a_value_error():
    with pytest.raises(ValueError, match="unknown candidate target"):
        validate_payload("bogus", {"intent": {"instructions": "x"}}, preflight_base())


# ------------------------------------------------------------------
# Text bounds: per-field length and the ≤40% change magnitude.
# ------------------------------------------------------------------


def test_instructions_over_the_length_limit_are_rejected():
    base = preflight_base()
    payload = full_payload(base, intent={"instructions": "x" * 501})
    with pytest.raises(PayloadBoundaryError, match="501"):
        validate_payload("preflight", payload, base)


def test_instructions_at_the_length_limit_are_accepted():
    base = {"intent": Noul(instructions="x" * 500)}
    assert validate_payload("preflight", {"intent": {"instructions": "x" * 499 + "y"}}, base) is None


def test_criteria_value_over_the_length_limit_is_rejected():
    base = preflight_base()
    payload = full_payload(
        base,
        intent={
            "criteria": {
                "structured_query": "y" * 201,
                "retrieval_qa": "可在授权知识库中检索并回答。",
                "tool_task": "需要受控工具或多步推理。",
                "clarification": "问题含糊，需要先澄清。",
            }
        },
    )
    with pytest.raises(PayloadBoundaryError, match="201"):
        validate_payload("preflight", payload, base)


def test_change_at_the_limit_is_accepted_and_one_character_over_is_rejected():
    """Deterministic bound: 40 of 100 characters may change, 41 may not."""
    base = {"intent": Noul(instructions="a" * 100)}
    at_limit = {"intent": {"instructions": "a" * 60 + "b" * 40}}
    over_limit = {"intent": {"instructions": "a" * 59 + "b" * 41}}

    assert validate_payload("preflight", at_limit, base) is None
    with pytest.raises(PayloadBoundaryError, match="change"):
        validate_payload("preflight", over_limit, base)


def test_criteria_text_uses_the_same_change_limit():
    base = {"intent": Choice(instructions="q", criteria={"a": "x" * 100})}
    at_limit = {"intent": {"criteria": {"a": "x" * 60 + "y" * 40}}}
    over_limit = {"intent": {"criteria": {"a": "x" * 59 + "y" * 41}}}

    assert validate_payload("preflight", at_limit, base) is None
    with pytest.raises(PayloadBoundaryError, match="change"):
        validate_payload("preflight", over_limit, base)


def test_a_full_rewrite_of_one_question_is_rejected():
    base = preflight_base()
    payload = full_payload(
        base,
        intent={"instructions": "这个问题的含义已经和原来完全不同，属于整题重写，边界必须拒绝。"},
    )
    with pytest.raises(PayloadBoundaryError, match="change"):
        validate_payload("preflight", payload, base)


def test_rejection_reasons_carry_no_question_text():
    """A rejected payload must not leak knowledge-base content into a reason."""
    marker = "TOPSECRET-不得外泄"
    base = {"intent": Noul(instructions="a" * 100)}
    with pytest.raises(PayloadBoundaryError) as excinfo:
        validate_payload("preflight", {"intent": {"instructions": "b" * 100}}, base)
    assert marker not in str(excinfo.value)

    with pytest.raises(PayloadBoundaryError) as length_excinfo:
        validate_payload("preflight", {"intent": {"instructions": marker + "z" * 600}}, base)
    assert marker not in str(length_excinfo.value)


# ------------------------------------------------------------------
# The frozen contract model.
# ------------------------------------------------------------------


def test_candidate_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        sample_candidate(unknown_field="x")


def test_candidate_requires_a_rollback_ref_to_the_current_version():
    with pytest.raises(ValidationError):
        sample_candidate(rollback_ref={"payload_sha256": "a" * 64})
    with pytest.raises(ValidationError):
        sample_candidate(rollback_ref={"question_set_version": "preflight-v1"})


def test_candidate_requires_a_hex_payload_hash():
    with pytest.raises(ValidationError):
        sample_candidate(rollback_ref={"question_set_version": "preflight-v1", "payload_sha256": "nope"})


def test_candidate_confidence_is_a_closed_set():
    for confidence in ("low", "medium", "high"):
        assert sample_candidate(confidence=confidence).confidence == confidence
    with pytest.raises(ValidationError):
        sample_candidate(confidence="certain")
    with pytest.raises(ValidationError):
        sample_candidate(target="prompts")


def test_candidate_is_frozen_and_keeps_artifact_and_target_closed():
    candidate = sample_candidate()
    assert candidate.artifact_type == "jev_question_set"
    assert candidate.target == "preflight"
    with pytest.raises(ValidationError):
        sample_candidate(artifact_type="prompt")
    with pytest.raises((TypeError, ValidationError)):
        candidate.confidence = "high"


def test_candidate_payload_shape_is_checked_at_construction():
    with pytest.raises(ValidationError):
        sample_candidate(payload={})
    with pytest.raises(ValidationError):
        sample_candidate(payload={"intent": {"instructions": 1}})
    with pytest.raises(ValidationError):
        sample_candidate(payload={"intent": {"type": "noul"}})
