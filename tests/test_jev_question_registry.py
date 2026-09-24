"""Task 5 (layer 4/5): the question-set registry, the text-only overlay, and provider injection.

The default question sets are the stable protocol the pipeline depends on: stable
ids, a version string, a primitive type, and full meanings in the text. This
file pins that a candidate question set may only ever be a **data** registration
next to those defaults and that ``build_with_overlay`` rewrites text on the very
question objects the default builders just produced — so the id set, the count
and the ``Choice``/``Noul`` primitives the deterministic mappers read can never
move.

The second half pins the ``JevClient`` injection seam: with no provider the
static module path is byte-for-byte the old behaviour; with a provider the
questions it returns are what goes on the wire and the version it returns is the
one written back into ``PreflightDecision.question_set_version`` (the value the
pipeline stamps into ``decision_metadata``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typesafe_sdk import Choice, Noul

from rag.jev.client import JevClient
from rag.jev.questions import (
    PASSAGE_QUESTION_SET_VERSION,
    PREFLIGHT_QUESTION_SET_VERSION,
    build_passage_questions,
    build_preflight_questions,
    build_with_overlay,
    question_set_registry,
    register_candidate_question_set,
    unregister_candidate_question_set,
)

CANDIDATE_VERSION = f"{PREFLIGHT_QUESTION_SET_VERSION}+candidate-7"
CANDIDATE_INTENT_INSTRUCTIONS = "判断用户问题的主要意图；只依据问题本身与知识库能力摘要。"


def snapshot(questions: dict) -> dict:
    """Text snapshot of one question set, used to prove only text moved."""
    return {
        question_id: {
            "type": type(question).__name__,
            "instructions": question.instructions,
            "criteria": dict(question.criteria) if question.criteria is not None else None,
        }
        for question_id, question in questions.items()
    }


def preflight_answers() -> dict:
    return {
        "intent": SimpleNamespace(choice="retrieval_qa", confidence=0.94),
        "complexity": SimpleNamespace(choice="simple", confidence=0.91),
        "needs_query_rewrite": SimpleNamespace(noul=0.08),
    }


def passage_answers(chunk_ids) -> dict:
    answers = {}
    for chunk_id in chunk_ids:
        for suffix in (
            "is_relevant",
            "contains_answer_evidence",
            "contradicts_query_premise",
            "contains_prompt_injection",
        ):
            answers[f"{chunk_id}.{suffix}"] = SimpleNamespace(noul=0.5)
    return answers


class RecordingClient:
    """A stub SDK client recording the questions it was asked to judge."""

    def __init__(self, answers):
        self.answers = answers
        self.questions_sent: list[dict] = []

    def system_one(self, state, questions, model=None):
        self.questions_sent.append(dict(questions))
        return SimpleNamespace(answers=self.answers, model="jev-latest")


# ------------------------------------------------------------------
# The registry: defaults are always there, candidates are data only.
# ------------------------------------------------------------------


def test_registry_holds_the_default_versions_and_nothing_else():
    registry = question_set_registry()

    assert set(registry) == {"preflight", "passages"}
    assert set(registry["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}
    assert registry["preflight"][PREFLIGHT_QUESTION_SET_VERSION] is build_preflight_questions
    assert set(registry["passages"]) == {PASSAGE_QUESTION_SET_VERSION}


def test_register_candidate_adds_only_the_candidate_version():
    def candidate_builder(*args, **kwargs):
        raise AssertionError("the stub builder is never called by the registry tests")

    register_candidate_question_set("preflight", CANDIDATE_VERSION, candidate_builder)
    try:
        registry = question_set_registry()
        assert registry["preflight"][CANDIDATE_VERSION] is candidate_builder
        assert registry["preflight"][PREFLIGHT_QUESTION_SET_VERSION] is build_preflight_questions
        assert set(registry["passages"]) == {PASSAGE_QUESTION_SET_VERSION}
        # A live view is returned; the caller cannot mutate the registry through it.
        registry["preflight"].pop(CANDIDATE_VERSION)
        assert CANDIDATE_VERSION in question_set_registry()["preflight"]
    finally:
        unregister_candidate_question_set("preflight", CANDIDATE_VERSION)

    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}


def test_register_rejects_an_unknown_target_and_the_default_version():
    with pytest.raises(ValueError):
        register_candidate_question_set("retrieval", "v1", build_preflight_questions)
    with pytest.raises(ValueError):
        register_candidate_question_set(
            "preflight", PREFLIGHT_QUESTION_SET_VERSION, build_preflight_questions
        )
    with pytest.raises(TypeError):
        register_candidate_question_set("preflight", CANDIDATE_VERSION, "not-callable")

    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}


def test_unregister_never_touches_the_defaults_and_is_idempotent():
    with pytest.raises(ValueError):
        unregister_candidate_question_set("preflight", PREFLIGHT_QUESTION_SET_VERSION)
    with pytest.raises(ValueError):
        unregister_candidate_question_set("passages", PASSAGE_QUESTION_SET_VERSION)

    unregister_candidate_question_set("preflight", f"{PREFLIGHT_QUESTION_SET_VERSION}+never-1")
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}


# ------------------------------------------------------------------
# build_with_overlay: text only, never a rebuilt question object.
# ------------------------------------------------------------------


def sample_payload() -> dict:
    """A bounded payload: one instruction edit, one criteria edit, one untouched."""
    return {
        "intent": {"instructions": CANDIDATE_INTENT_INSTRUCTIONS},
        "complexity": {
            "criteria": {
                "simple": "单一步骤即可直接回答，无需工具。",
                "multi_step": "需要多个步骤或整合多个来源的信息。",
                "specialist": "需要专业领域知识或复杂推理。",
            }
        },
        "needs_query_rewrite": {},
    }


def test_overlay_rewrites_only_text_and_keeps_the_question_objects(monkeypatch):
    import rag.jev.questions as questions

    # A stable base set makes the identity requirement checkable: the overlay
    # must serve the very objects the builder produced, never rebuild them.
    original_builder = questions.build_preflight_questions
    produced = original_builder()
    before = snapshot(produced)
    monkeypatch.setattr(questions, "build_preflight_questions", lambda: produced)

    overlaid = build_with_overlay("preflight", sample_payload())

    assert overlaid is produced
    for question_id, question in overlaid.items():
        assert question is produced[question_id]
        assert isinstance(question, Choice | Noul)
    after = snapshot(overlaid)
    assert after["intent"]["instructions"] == CANDIDATE_INTENT_INSTRUCTIONS
    assert after["intent"]["instructions"] != before["intent"]["instructions"]
    assert after["complexity"]["criteria"]["simple"] == "单一步骤即可直接回答，无需工具。"
    assert after["complexity"]["criteria"]["simple"] != before["complexity"]["criteria"]["simple"]
    # Untouched question and id set stay exactly as they were.
    assert after["needs_query_rewrite"] == before["needs_query_rewrite"]
    assert set(after["intent"]["criteria"]) == set(before["intent"]["criteria"])
    # A freshly built default set is pristine: the overlay never leaks into it,
    # neither into the module-level criteria dicts nor into the builder's output.
    pristine = original_builder()
    assert snapshot(pristine) == before
    assert pristine["complexity"].criteria is not overlaid["complexity"].criteria


def test_overlay_rejects_an_unknown_question_id():
    with pytest.raises(KeyError):
        build_with_overlay("preflight", {"unknown_id": {"instructions": "x"}})


def test_overlay_rejects_an_unknown_target():
    with pytest.raises(ValueError):
        build_with_overlay("retrieval", {"intent": {"instructions": "x"}})


def test_overlay_applies_to_the_passage_set_of_one_request():
    chunk_ids = ["c0"]
    base = build_passage_questions(chunk_ids)
    before = snapshot(base)

    overlaid = build_with_overlay(
        "passages",
        {"c0.is_relevant": {"instructions": "判断 passages.c0.text 是否切题。"}},
        chunk_ids,
    )

    assert set(overlaid) == set(base)
    assert isinstance(overlaid["c0.is_relevant"], Noul)
    assert overlaid["c0.is_relevant"].instructions == "判断 passages.c0.text 是否切题。"
    # The other three questions of that chunk keep their default text, and the
    # default set itself stays pristine for the next request.
    for suffix in (
        "contains_answer_evidence",
        "contradicts_query_premise",
        "contains_prompt_injection",
    ):
        assert overlaid[f"c0.{suffix}"].instructions == before[f"c0.{suffix}"]["instructions"]
    assert snapshot(build_passage_questions(chunk_ids)) == before


def test_overlay_needs_the_chunk_ids_of_the_request_it_serves():
    with pytest.raises(KeyError):
        build_with_overlay("passages", {"c0.is_relevant": {"instructions": "x"}})
    assert build_with_overlay("passages", {}, ["c0"]) == build_passage_questions(["c0"])


# ------------------------------------------------------------------
# The JevClient seam: None keeps the static path, a provider replaces it.
# ------------------------------------------------------------------


def test_without_a_provider_the_static_path_is_unchanged():
    client = RecordingClient(preflight_answers())

    result = JevClient(client=client).preflight({"question": "年假几天"})

    assert result.available is True
    assert result.value.question_set_version == PREFLIGHT_QUESTION_SET_VERSION
    assert set(client.questions_sent[0]) == {"intent", "complexity", "needs_query_rewrite"}
    assert client.questions_sent[0]["intent"].instructions == (
        build_preflight_questions()["intent"].instructions
    )


def test_a_provided_question_set_is_sent_and_its_version_is_written_back():
    seen: list[tuple[str, list[str] | None]] = []

    def provider(stage: str, chunk_ids: list[str] | None):
        seen.append((stage, chunk_ids))
        questions = build_preflight_questions()
        questions["intent"].instructions = CANDIDATE_INTENT_INSTRUCTIONS
        return questions, CANDIDATE_VERSION

    client = RecordingClient(preflight_answers())
    result = JevClient(client=client, question_set_provider=provider).preflight(
        {"question": "年假几天"}
    )

    assert seen == [("preflight", None)]
    assert result.value.question_set_version == CANDIDATE_VERSION
    assert client.questions_sent[0]["intent"].instructions == CANDIDATE_INTENT_INSTRUCTIONS


def test_the_passages_provider_receives_the_chunk_ids_of_the_request():
    seen: list[tuple[str, list[str] | None]] = []

    def provider(stage: str, chunk_ids: list[str] | None):
        seen.append((stage, chunk_ids))
        return build_passage_questions(chunk_ids or []), "passages-v1+candidate-7"

    client = RecordingClient(passage_answers(["c0", "c1"]))
    result = JevClient(client=client, question_set_provider=provider).judge_passages(
        {"question": "年假几天", "passages": {"c0": {"text": "t"}, "c1": {"text": "u"}}},
        ["c0", "c1"],
    )

    assert seen == [("passages", ["c0", "c1"])]
    assert result.available is True
    assert [judgment.chunk_id for judgment in result.value] == ["c0", "c1"]


def test_an_empty_chunk_list_never_consults_the_provider():
    seen: list[tuple[str, list[str] | None]] = []

    def provider(stage: str, chunk_ids: list[str] | None):
        seen.append((stage, chunk_ids))
        return {}, "unused"

    result = JevClient(client=RecordingClient({}), question_set_provider=provider).judge_passages(
        {"question": "年假几天", "passages": {}}, []
    )

    assert result.available is False
    assert result.failure_kind == "invalid_response"
    assert seen == []


def test_a_failing_provider_is_a_normalized_failure_without_calling_the_sdk():
    def broken_provider(stage: str, chunk_ids: list[str] | None):
        raise KeyError("intent")

    client = RecordingClient(preflight_answers())
    result = JevClient(client=client, question_set_provider=broken_provider).preflight(
        {"question": "年假几天"}
    )

    assert result.available is False
    assert result.failure_kind == "invalid_response"
    assert client.questions_sent == []
