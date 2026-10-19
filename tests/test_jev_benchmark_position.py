"""Answer-position scoring: 证据不足就拒答，该作答就必须作答。

产品口径（2026-10-19）：证据不足时拒答是正确行为。但"拒答"此前在基准里**没有任何记分位置**——
路由标签只有 rag/structured/tool/clarification 四档，`route_correct` 只比较链路，
所以一次"该作答却拒答"和一次"该拒答却作答"都不改变任何数字。这组测试钉住补上的那一档。

设计约束（不得违反）：
* `expected_position` 是**加**在用例上的新字段，不替代 `expected_route`；缺省为空 = 不记分，
  既有 20 条 A/B 用例与 24 条真实用例的记分结果逐项不变。
* 该拒绝答却作答（幻觉风险）与该作答却拒答（假拒答）**都是错**，各自单列计数。
* 没有任何 position 用例时，`refusal` 判据是 advisory，不因"没测"而失败。
"""

from __future__ import annotations

from scripts.benchmark_jev_retrieval import (
    BenchmarkCase,
    PathMetrics,
    PreparationOutcome,
    evaluate_acceptance,
    load_cases,
    run_benchmark,
)

ANSWER = "answer"
REFUSE = "refuse"


def case(case_id: str, position: str = "") -> BenchmarkCase:
    return BenchmarkCase(
        id=case_id,
        question=f"问题 {case_id}",
        expected_route="rag",
        kb_id="kb_accessories",
        expected_source_names=(),
        expected_position=position,
    )


def outcome(answerability: str | None) -> PreparationOutcome:
    return PreparationOutcome(
        preparation_ms=100.0,
        route="rag",
        source_names=(),
        answerability=answerability,
    )


def path_metrics(**overrides) -> PathMetrics:
    values = {
        "sample_count": 4,
        "prep_p50_ms": 100.0,
        "prep_p95_ms": 200.0,
        "rewrite_rate": 0.0,
        "route_accuracy": 1.0,
        "top1_hit_rate": 0.0,
        "top5_hit_rate": 0.0,
        "external_calls_per_query": 2.0,
        "failed_calls": 0,
    }
    values.update(overrides)
    return PathMetrics(**values)


def _run(cases, old_answerability, new_answerability, runs: int = 1):
    responses = {
        "old": old_answerability,
        "new": new_answerability,
    }
    return run_benchmark(
        cases,
        lambda item: outcome(responses["old"](item)),
        lambda item: outcome(responses["new"](item)),
        runs=runs,
        warmups=0,
    )


# ------------------------------------------------------------------
# The field itself: it is additive, and a typo is a hard failure.
# ------------------------------------------------------------------


def test_position_is_optional_and_defaults_to_unscored():
    unscored = case("real-001")
    assert unscored.expected_position == ""


def test_unknown_position_is_rejected_like_an_unknown_route(tmp_path):
    import json

    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "real-001",
                        "question": "q",
                        "expected_route": "rag",
                        "kb_id": "kb_accessories",
                        "expected_position": "maybe",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    import pytest

    with pytest.raises(ValueError, match="unknown expected_position"):
        load_cases(path)


# ------------------------------------------------------------------
# Scoring: both directions are errors, and each is counted on its own.
# ------------------------------------------------------------------


def test_refusing_where_the_label_demands_an_answer_is_a_miss():
    report = _run(
        [case("real-020", ANSWER)],
        old_answerability=lambda _c: "insufficient_evidence",
        new_answerability=lambda _c: "insufficient_evidence",
    )

    assert report.new.refusal_misses == 1
    assert report.new.refusal_escapes == 0
    assert report.new.position_accuracy == 0.0
    assert "refusal" in report.verdict.failures


def test_answering_where_the_label_demands_a_refusal_is_an_escape():
    report = _run(
        [case("real-012", REFUSE)],
        old_answerability=lambda _c: "answerable",
        new_answerability=lambda _c: "answerable",
    )

    assert report.new.refusal_escapes == 1
    assert report.new.refusal_misses == 0
    assert "refusal" in report.verdict.failures


def test_correct_positions_pass_and_are_counted():
    report = _run(
        [case("real-020", ANSWER), case("real-012", REFUSE)],
        old_answerability=lambda _c: "answerable",
        new_answerability=lambda item: "answerable" if item.id == "real-020" else "insufficient_evidence",
    )

    assert (report.new.refusal_escapes, report.new.refusal_misses) == (0, 0)
    assert report.new.position_accuracy == 1.0
    assert "refusal" not in report.verdict.failures


def test_the_new_label_and_the_route_label_are_independent():
    """同样走 rag：一条要求作答、一条要求拒答，路由都算对。"""
    report = _run(
        [case("real-020", ANSWER), case("real-012", REFUSE)],
        old_answerability=lambda _c: "insufficient_evidence",
        new_answerability=lambda _c: "insufficient_evidence",
    )

    assert report.new.route_accuracy == 1.0
    assert report.new.refusal_misses == 1
    assert report.new.refusal_escapes == 0


# ------------------------------------------------------------------
# Which decisions count as "answered": clarification is not an answer.
# ------------------------------------------------------------------


def test_only_answerable_decisions_count_as_answering():
    answered = _run(
        [case("real-020", ANSWER)],
        old_answerability=lambda _c: "answerable_with_warning",
        new_answerability=lambda _c: "answerable_with_warning",
    )
    # 有历史证据时的限定回答仍是一次作答，落在历史版本上属于证据问题，不在这里判错。
    assert answered.new.refusal_misses == 0

    refused = _run(
        [case("real-020", ANSWER)],
        old_answerability=lambda _c: "clarification_required",
        new_answerability=lambda _c: "clarification_required",
    )
    # 澄清与权限受限都**没有给出答案**，不能算作答。
    assert refused.new.refusal_misses == 1


def test_a_crashed_preparation_is_neither_answered_nor_refused():
    report = run_benchmark(
        [case("real-020", ANSWER)],
        lambda _c: PreparationOutcome(preparation_ms=0.0, route="error", failure="timeout"),
        lambda _c: PreparationOutcome(preparation_ms=0.0, route="error", failure="timeout"),
        runs=1,
        warmups=0,
    )

    # 崩溃已经由 route 判据算错，不该再被重复记成"假拒答"。
    assert report.new.refusal_misses == 0
    assert report.new.refusal_case_count == 0


def test_a_structured_clarification_is_not_an_answer():
    """结构化澄清没有 ``decision``，但它的答案是"请指明字段"这类追问，不是回答。

    2026-10-19 实测：``找一下副厂配件明细`` 在结构化路由上会落到结构化澄清（5 个来源、
    无 ``decision``）。若把"没有判定"直接当作作答，一次追问就会被记成"该作答却拒答"，
    而它其实只是没答——两者都不该在这里记成"答了"。
    """

    def structured_clarification(_case):
        return PreparationOutcome(
            preparation_ms=100.0,
            route="structured",
            source_names=(),
            answerability=None,
        )

    report = run_benchmark(
        [case("real-020", ANSWER)],
        structured_clarification,
        structured_clarification,
        runs=1,
        warmups=0,
    )

    assert report.new.refusal_misses == 1
    assert report.new.position_accuracy == 0.0


def test_a_path_that_reports_no_answerability_at_all_is_rejected_not_scored():
    """带标签却读不到位置判定的采样，必须**硬失败**,不能被默认成"拒答"或"作答"。

    这与上一条的区别是:结构化澄清是**已知的追问**（route=structured,没有 decision）,
    可以正常记分；而这里是一种无从判断的形态（route=rag 却没有 decision），
    替它下任何结论都是编数字。
    """
    import pytest

    with pytest.raises(ValueError, match="no answerability decision and no known-unanswered route"):
        run_benchmark(
            [case("real-020", ANSWER)],
            lambda _c: PreparationOutcome(preparation_ms=100.0, route="rag", answerability=None),
            lambda _c: PreparationOutcome(preparation_ms=100.0, route="rag", answerability=None),
            runs=1,
            warmups=0,
        )


# ------------------------------------------------------------------
# Acceptance: not tested is not the same as failed.
# ------------------------------------------------------------------


def test_refusal_check_is_advisory_when_no_case_carries_a_position():
    report = _run(
        [case("rag-001")],
        old_answerability=lambda _c: "answerable",
        new_answerability=lambda _c: "answerable",
    )

    check = next(item for item in report.verdict.checks if item.name == "refusal")
    assert check.advisory is True
    assert check.passed is True


def test_refusal_check_is_a_hard_criterion_once_a_case_carries_a_position():
    verdict = evaluate_acceptance(
        path_metrics(),
        path_metrics(refusal_escapes=1, refusal_case_count=3, position_accuracy=0.667),
        profile="single_turn",
    )

    check = next(item for item in verdict.checks if item.name == "refusal")
    assert check.advisory is False
    assert check.passed is False
    assert "refusal" in verdict.failures


def test_refusal_counts_survive_the_json_report():
    report = _run(
        [case("real-012", REFUSE)],
        old_answerability=lambda _c: "answerable",
        new_answerability=lambda _c: "answerable",
    )

    payload = report.to_dict()
    assert payload["new"]["refusal_escapes"] == 1
    assert payload["new"]["refusal_misses"] == 0
    assert payload["new"]["refusal_case_count"] == 1
