"""Tests for the deterministic preflight routing policy and service facade."""

import pytest

from rag.jev.models import JevBatchResult, PreflightDecision
from rag.jev.policy import JevPolicy, KbCapabilities, RouteDecision, choose_route
from rag.jev.service import JevDecisionService


def preflight(intent: str, confidence: float, rewrite_probability: float = 0.0) -> PreflightDecision:
    return PreflightDecision(
        intent=intent,
        intent_confidence=confidence,
        complexity="simple",
        complexity_confidence=confidence,
        rewrite_probability=rewrite_probability,
        model="jev-latest",
        question_set_version="preflight-v1",
        duration_ms=1.0,
    )


@pytest.mark.parametrize(
    ("intent", "confidence", "expected"),
    [
        ("structured_query", 0.95, "structured"),
        ("retrieval_qa", 0.95, "rag"),
        ("tool_task", 0.95, "tool"),
        ("clarification", 0.95, "clarification"),
        ("tool_task", 0.40, "legacy"),
    ],
)
def test_route_policy_is_confidence_gated(intent, confidence, expected):
    decision = preflight(intent=intent, confidence=confidence)
    assert choose_route(decision, threshold=0.75).route == expected


def test_missing_structured_capability_prevents_structured_route():
    decision = preflight("structured_query", 0.95)
    result = choose_route(
        decision,
        capabilities=KbCapabilities(has_structured_data=False),
        threshold=0.75,
    )
    assert result.route == "legacy"
    assert "structured" in result.reason


def test_low_confidence_clarification_defaults_to_rag():
    decision = preflight("clarification", 0.40)
    assert choose_route(decision, threshold=0.75).route == "rag"


def test_low_confidence_retrieval_defaults_to_rag():
    decision = preflight("retrieval_qa", 0.40)
    result = choose_route(decision, threshold=0.75)
    assert result.route == "rag"
    assert "defaults_rag" in result.reason


def test_low_confidence_structured_falls_back_to_legacy():
    decision = preflight("structured_query", 0.40)
    assert choose_route(decision, threshold=0.75).route == "legacy"


def test_should_rewrite_only_above_configured_threshold():
    policy = JevPolicy(route_confidence=0.75, rewrite_probability=0.5)
    high = choose_route(preflight("retrieval_qa", 0.95, rewrite_probability=0.8), policy=policy)
    assert high.should_rewrite is True
    low = choose_route(preflight("retrieval_qa", 0.95, rewrite_probability=0.2), policy=policy)
    assert low.should_rewrite is False


def test_route_decision_carries_preflight_for_trace():
    decision = preflight("retrieval_qa", 0.95)
    result = choose_route(decision, threshold=0.75)
    assert isinstance(result, RouteDecision)
    assert result.preflight is decision
    assert result.reason


class FakeJevClient:
    def __init__(self, result: JevBatchResult):
        self.result = result
        self.states: list[dict] = []

    def preflight(self, state: dict) -> JevBatchResult[PreflightDecision]:
        self.states.append(state)
        return self.result


def test_unavailable_preflight_returns_legacy():
    client = FakeJevClient(JevBatchResult(available=False, failure_kind="timeout", duration_ms=1.0))
    service = JevDecisionService(client=client)
    decision = service.route("年假几天", KbCapabilities())
    assert decision.route == "legacy"
    assert decision.should_rewrite is False
    assert decision.preflight is None
    assert "timeout" in decision.reason


def test_service_route_applies_policy_and_sends_safe_state():
    client = FakeJevClient(
        JevBatchResult(
            value=preflight("retrieval_qa", 0.95, rewrite_probability=0.8),
            available=True,
            duration_ms=1.0,
        )
    )
    service = JevDecisionService(client=client, policy=JevPolicy(route_confidence=0.75, rewrite_probability=0.5))
    decision = service.route("年假几天", KbCapabilities(has_structured_data=False, allowed_tools=("calc",)))
    assert decision.route == "rag"
    assert decision.should_rewrite is True
    assert decision.preflight is not None
    sent = client.states[0]
    assert sent["question"] == "年假几天"
    assert sent["capabilities"] == {"structured_data_available": False, "tools_available": ["calc"]}
