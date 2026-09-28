"""Tests for the fault-isolated TypeSafe adapter."""

import logging
from types import SimpleNamespace

import httpx2
import pytest
from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeRateLimitError,
)

import rag.jev.client as client_module
from config import settings
from rag.jev.client import JevClient
from rag.jev.questions import PASSAGE_QUESTION_SET_VERSION

FAKE_KEY = "tsa-fake-key-do-not-log-123"


class FakeChoice:
    def __init__(self, choice: str, confidence: float):
        self.choice = choice
        self.confidence = confidence
        self.probabilities = {choice: confidence}


class FakeNoul:
    def __init__(self, noul: float):
        self.noul = noul


class FakeTypesafeClient:
    def __init__(self):
        self.answers = {}
        self.error = None
        self.calls = 0

    def system_one(self, state, questions, model=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(answers=self.answers, model="jev-latest")


@pytest.fixture
def fake_typesafe_client():
    return FakeTypesafeClient()


def preflight_answers():
    return {
        "intent": FakeChoice("retrieval_qa", confidence=0.94),
        "complexity": FakeChoice("simple", confidence=0.91),
        "needs_query_rewrite": FakeNoul(0.08),
    }


def passage_answers(chunk_ids):
    answers = {}
    for chunk_id in chunk_ids:
        answers[f"{chunk_id}.is_relevant"] = FakeNoul(0.9)
        answers[f"{chunk_id}.contains_answer_evidence"] = FakeNoul(0.8)
        answers[f"{chunk_id}.contradicts_query_premise"] = FakeNoul(0.1)
        answers[f"{chunk_id}.contains_prompt_injection"] = FakeNoul(0.0)
    return answers


def test_preflight_maps_choice_score_and_noul(fake_typesafe_client):
    fake_typesafe_client.answers = preflight_answers()
    result = JevClient(client=fake_typesafe_client).preflight({"question": "年假几天"})
    assert result.available is True
    assert result.failure_kind is None
    assert result.value.intent == "retrieval_qa"
    assert result.value.intent_confidence == 0.94
    assert result.value.complexity == "simple"
    assert result.value.complexity_confidence == 0.91
    assert result.value.rewrite_probability == 0.08
    assert result.value.model == "jev-latest"
    assert result.value.question_set_version == "preflight-v1"
    assert result.duration_ms >= 0.0


def test_missing_key_returns_disabled_without_constructing_sdk(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    constructed = []

    def fail_constructor(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("SDK client must not be constructed without a key")

    monkeypatch.setattr(client_module, "TypeSafeClient", fail_constructor)
    result = JevClient().preflight({"question": "test"})
    assert result.available is False
    assert result.failure_kind == "disabled"
    assert result.value is None
    assert constructed == []


def test_lazy_client_is_constructed_once_and_reused(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", FAKE_KEY)
    instances = []

    class RecordingClient(FakeTypesafeClient):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.answers = preflight_answers()
            instances.append(self)

    monkeypatch.setattr(client_module, "TypeSafeClient", RecordingClient)
    jev = JevClient()
    first = jev.preflight({"question": "年假几天"})
    second = jev.preflight({"question": "年假几天"})
    assert first.available is True and second.available is True
    assert len(instances) == 1


@pytest.mark.parametrize(
    ("error", "expected_kind"),
    [
        (TypeSafeAPITimeoutError(1.5), "timeout"),
        (TypeSafeRateLimitError(429, None, httpx2.Headers()), "rate_limited"),
        (TypeSafeAuthenticationError(401, None, httpx2.Headers()), "auth"),
        (TypeSafeAPIError(503, None, httpx2.Headers()), "service"),
        (TypeSafeAPIConnectionError(), "service"),
        (TypeSafeAPIResponseValidationError(200, None, httpx2.Headers(), "answers.intent"), "invalid_response"),
    ],
)
def test_sdk_failures_map_to_normalized_kinds(fake_typesafe_client, error, expected_kind):
    fake_typesafe_client.error = error
    result = JevClient(client=fake_typesafe_client).preflight({"question": "年假几天"})
    assert result.available is False
    assert result.failure_kind == expected_kind
    assert result.value is None


def test_missing_answer_id_is_invalid_response(fake_typesafe_client):
    answers = preflight_answers()
    del answers["complexity"]
    fake_typesafe_client.answers = answers
    result = JevClient(client=fake_typesafe_client).preflight({"question": "年假几天"})
    assert result.available is False
    assert result.failure_kind == "invalid_response"


def test_out_of_range_confidence_is_invalid_response(fake_typesafe_client):
    fake_typesafe_client.answers = {
        "intent": FakeChoice("retrieval_qa", confidence=1.5),
        "complexity": FakeChoice("simple", confidence=0.91),
        "needs_query_rewrite": FakeNoul(0.08),
    }
    result = JevClient(client=fake_typesafe_client).preflight({"question": "年假几天"})
    assert result.available is False
    assert result.failure_kind == "invalid_response"


def test_passage_judgments_map_all_questions(fake_typesafe_client):
    fake_typesafe_client.answers = passage_answers(["c0", "c1"])
    result = JevClient(client=fake_typesafe_client).judge_passages(
        {"question": "年假几天", "passages": {"c0": {"text": "t"}, "c1": {"text": "u"}}},
        ["c0", "c1"],
    )
    assert result.available is True
    assert [judgment.chunk_id for judgment in result.value] == ["c0", "c1"]
    assert result.value[0].relevance == 0.9
    assert result.value[0].answer_evidence == 0.8
    assert result.value[0].contradiction == 0.1
    assert result.value[0].prompt_injection == 0.0


def test_passage_judgments_carry_the_question_set_version(fake_typesafe_client):
    """Every judgment names the question set that produced it.

    The preflight decision already carries its version; the passages stage has to
    carry its own, or a request judged with a candidate passage set cannot be
    attributed to that candidate in ``decision_metadata``.
    """
    fake_typesafe_client.answers = passage_answers(["c0", "c1"])
    result = JevClient(client=fake_typesafe_client).judge_passages(
        {"question": "年假几天", "passages": {"c0": {"text": "t"}, "c1": {"text": "u"}}},
        ["c0", "c1"],
    )
    assert result.available is True
    assert [judgment.question_set_version for judgment in result.value] == [
        PASSAGE_QUESTION_SET_VERSION,
        PASSAGE_QUESTION_SET_VERSION,
    ]


def test_passage_response_missing_one_question_is_invalid_response(fake_typesafe_client):
    answers = passage_answers(["c0", "c1"])
    del answers["c1.contains_prompt_injection"]
    fake_typesafe_client.answers = answers
    result = JevClient(client=fake_typesafe_client).judge_passages(
        {"question": "年假几天", "passages": {"c0": {"text": "t"}, "c1": {"text": "u"}}},
        ["c0", "c1"],
    )
    assert result.available is False
    assert result.failure_kind == "invalid_response"


def test_open_circuit_returns_service_without_calling_sdk(fake_typesafe_client):
    fake_typesafe_client.error = TypeSafeAPIError(503, None, httpx2.Headers())
    jev = JevClient(client=fake_typesafe_client)
    for _ in range(5):
        result = jev.preflight({"question": "年假几天"})
        assert result.failure_kind == "service"
    assert fake_typesafe_client.calls == 5
    blocked = jev.preflight({"question": "年假几天"})
    assert blocked.available is False
    assert blocked.failure_kind == "service"
    assert fake_typesafe_client.calls == 5


def test_successful_call_closes_failure_streak(fake_typesafe_client):
    fake_typesafe_client.error = TypeSafeAPIError(503, None, httpx2.Headers())
    jev = JevClient(client=fake_typesafe_client)
    for _ in range(4):
        assert jev.preflight({"question": "q"}).failure_kind == "service"
    fake_typesafe_client.error = None
    fake_typesafe_client.answers = preflight_answers()
    assert jev.preflight({"question": "q"}).available is True
    fake_typesafe_client.error = TypeSafeAPIError(503, None, httpx2.Headers())
    for _ in range(4):
        assert jev.preflight({"question": "q"}).failure_kind == "service"
    # One success reset the streak, so the fifth failure must not open the circuit.
    assert jev.preflight({"question": "q"}).failure_kind == "service"
    assert fake_typesafe_client.calls == 10


def test_failure_logs_do_not_leak_the_api_key(fake_typesafe_client, caplog):
    fake_typesafe_client.error = TypeSafeAuthenticationError(
        401, f"invalid key {FAKE_KEY}", httpx2.Headers()
    )
    with caplog.at_level(logging.WARNING, logger="rag.jev.client"):
        result = JevClient(client=fake_typesafe_client).preflight({"question": "年假几天"})
    assert result.failure_kind == "auth"
    assert FAKE_KEY not in caplog.text
