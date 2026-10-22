"""Tests for the deterministic preflight routing policy and service facade."""

import pytest

from rag.jev.models import JevBatchResult, PreflightDecision
from rag.jev.policy import (
    JevPolicy,
    KbCapabilities,
    RouteDecision,
    choose_route,
    deterministic_route,
    is_self_contained_lookup,
)
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


def test_a_bare_model_name_is_a_clarification():
    """``r50`` alone has no attribute; asking which one beats answering a nearby doc."""
    result = deterministic_route("r50", KbCapabilities(has_structured_data=True))
    assert result is not None
    assert result.route == "clarification"
    assert result.reason == "model_only_query"


def test_a_model_with_an_attribute_keeps_the_normal_path():
    assert deterministic_route("r50电池价格", KbCapabilities(has_structured_data=True)) is None


def test_an_aggregate_question_with_structured_data_is_structured():
    result = deterministic_route(
        "帮我统计7月份的招聘数量及入职人数",
        KbCapabilities(has_structured_data=True),
    )
    assert result is not None
    assert result.route == "structured"
    assert result.reason == "structured_aggregate_question"


def test_an_aggregate_question_without_structured_data_is_not_forced():
    assert deterministic_route(
        "帮我统计7月份的招聘数量",
        KbCapabilities(has_structured_data=False),
    ) is None


def test_specific_lookup_detection():
    assert is_self_contained_lookup("r50电池价格")
    assert is_self_contained_lookup("富士 X-S20 电池和充电器是什么型号")
    assert not is_self_contained_lookup("机器哪里发货呢")
    assert not is_self_contained_lookup("r50")
    assert not is_self_contained_lookup("人员盘点的范围包括哪些人员？")


def test_specific_lookup_suppresses_rewrite():
    client = FakeJevClient(
        JevBatchResult(
            value=preflight("retrieval_qa", 0.95, rewrite_probability=0.9),
            available=True,
            duration_ms=1.0,
        )
    )
    service = JevDecisionService(
        client=client,
        policy=JevPolicy(route_confidence=0.75, rewrite_probability=0.5),
    )
    decision = service.route("r50电池价格", KbCapabilities(has_structured_data=True))
    assert decision.route == "rag"
    assert decision.should_rewrite is False
    assert "specific_lookup" in decision.reason


def test_vague_question_keeps_rewrite():
    client = FakeJevClient(
        JevBatchResult(
            value=preflight("retrieval_qa", 0.95, rewrite_probability=0.9),
            available=True,
            duration_ms=1.0,
        )
    )
    service = JevDecisionService(
        client=client,
        policy=JevPolicy(route_confidence=0.75, rewrite_probability=0.5),
    )
    decision = service.route("机器哪里发货呢", KbCapabilities(has_structured_data=True))
    assert decision.should_rewrite is True


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
    assert sent["capabilities"] == {
        "structured_data_available": False,
        "tools_available": ["calc"],
        "structured_fields": [],
    }


def test_service_route_sends_the_field_names_the_judge_needs():
    """判定器要能看出"问题指向表格里的某一列"，否则字段类问题会被判成检索意图。

    2026-10-19 实测：`佳能 EOS R8 的有效像素是多少？` 在只有布尔能力摘要时被判成检索
    （`retrieval_confident`，confidence 0.83），结构化路由因此进不去；把**已按权限过滤**的字段名
    放进 state 后同一问题能进结构化路由。字段名不是新暴露的信息：它们来自调用者本就有权读的数据集。
    """
    client = FakeJevClient(
        JevBatchResult(
            value=preflight("structured_query", 0.9, rewrite_probability=0.1),
            available=True,
            duration_ms=1.0,
        )
    )
    service = JevDecisionService(client=client, policy=JevPolicy(route_confidence=0.75))
    capabilities = KbCapabilities(
        has_structured_data=True, structured_fields=("有效像素", "连拍速度"),
    )

    decision = service.route("佳能 EOS R8 的有效像素是多少？", capabilities)

    assert decision.route == "structured"
    assert client.states[0]["capabilities"]["structured_fields"] == ["有效像素", "连拍速度"]
