"""Task 3: the layer-2 benchmark gate — thresholds, per-case diff, state advance.

Layer 2 replays a candidate over the fixed A/B corpus with the live question set
as the baseline. The decision here is a pure function of two injected route
callables and the labeled cases, so the thresholds, the per-case regression
diff, and the store round-trip are all pinned without a single model call: the
callables answer from dictionaries, and the store is a temporary SQLite file.

The tests also nail the acceptance thresholds themselves (design §13, the same
ones the phase-2 A/B benchmark uses): route accuracy may not drop at all
(≤0pp) and the Top-5 source hit rate may drop at most 2pp. A later "fix" that
loosens a constant to let a candidate through fails here.
"""

import time

import pytest

from rag.evolution.candidate_gates import (
    GATE_NAME,
    ROUTE_DELTA_MAX_PP,
    TOP5_DELTA_MAX_PP,
    GateResult,
    candidate_from_row,
    evaluate_on_benchmark,
    evaluate_repeatable_on_benchmark,
    run_benchmark_gate,
)
from rag.evolution.candidate_store import CandidateStore
from rag.evolution.candidates import JevQuestionSetCandidate
from rag.evolution.models import EvolutionStatus, FailureObservation, ObservationPool
from rag.evolution.store import FailureTraceStore
from rag.jev.questions import PREFLIGHT_QUESTION_SET_VERSION, build_preflight_questions

PER_CASE_KEYS = {
    "case_id",
    "category",
    "expected_route",
    "baseline",
    "candidate",
    "route_changed",
    "route_regressed",
    "route_improved",
    "top5_regressed",
    "top5_improved",
    "refusal_escaped",
    "refusal_recovered",
}
SIDE_KEYS = {
    "route",
    "route_correct",
    "sources",
    "top5_hit",
    "external_calls",
    "duration_ms",
    "error",
    "answered",
}
STATS_KEYS = {
    "cases",
    "baseline",
    "candidate",
    "route_delta_pp",
    "top5_delta_pp",
    "route_regressions",
    "top5_regressions",
    "refusal_escapes",
    "refusal_misses",
    "improvements",
    "external_calls_delta",
    "thresholds",
    "candidate_ref",
}
SIDE_STATS_KEYS = {
    "route_hits",
    "route_accuracy",
    "top5_hits",
    "top5_labeled",
    "top5_hit_rate",
    "errors",
    "external_calls",
    "duration_ms",
}


# ------------------------------------------------------------------
# Local stand-ins for the phase-3 setup and the candidate contract.
# ------------------------------------------------------------------


def trace_store(tmp_path) -> FailureTraceStore:
    return FailureTraceStore(tmp_path / "evolution.db")


def make_approved_cluster(store: FailureTraceStore, key: str = "approved-1"):
    """One cluster promoted to ``approved_for_candidate`` with one member."""
    now = time.time()
    cluster = store.upsert_cluster(
        cluster_key=key,
        kb_id="kb1",
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        seen_at=now,
    )
    member = store.add_observation(
        FailureObservation(
            kb_id="kb1",
            failure_stage="retrieval",
            question="年假额度怎么算",
            route="rag",
            chunk_hashes=("a" * 64,),
            best_score=0.05,
            answerability_kind="insufficient_evidence",
        )
    )
    store.attach_cluster_member(
        member["id"],
        cluster["id"],
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        now=now,
    )
    admitted = store.promote_cluster(
        cluster["id"],
        new_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.CLUSTERED.value,
        now=now,
    )
    decided = store.decide_cluster(
        admitted["id"],
        new_status=EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        now=now,
    )
    return decided, member


def seeded(tmp_path):
    trace = trace_store(tmp_path)
    cluster, member = make_approved_cluster(trace)
    return trace, CandidateStore(trace), cluster, member


def gate_candidate(**kw) -> JevQuestionSetCandidate:
    """A bounded preflight candidate over the live question set."""
    base = build_preflight_questions()
    payload = {
        "intent": {"instructions": "判断用户问题的主要意图；只依据问题本身和提供的知识库能力摘要。"},
        "complexity": {},
        "needs_query_rewrite": {},
    }
    data = dict(
        artifact_type="jev_question_set",
        target="preflight",
        payload=payload,
        rationale="intent 判据在结构化查询边界与线上分歧集中",
        expected_effect="结构化查询意图判定更稳定",
        rollback_ref={
            "question_set_version": PREFLIGHT_QUESTION_SET_VERSION,
            "payload_sha256": "b" * 64,
        },
        confidence="medium",
    )
    data.update(kw)
    candidate = JevQuestionSetCandidate(**data)
    assert all(question_id in base for question_id in payload)
    return candidate


def gate_case(case_id, *, expected_route="rag", sources=("压测.md",), category="ordinary"):
    """One labeled benchmark case; the question text is the case id."""
    return {
        "id": case_id,
        "question": case_id,
        "expected_route": expected_route,
        "kb_id": "rag_docs",
        "expected_source_names": list(sources),
        "category": category,
    }


def route_fn(routes, *, sources=None, error_on=(), external_calls=0, duration_ms=0.0, answerability=None):
    """A deterministic route callable shaped like the gate's injection contract.

    ``answerability`` maps question -> ``prepared["decision"].kind``; a question missing from it
    reports no decision at all (the legacy path builds no evidence bundle).
    """
    sources = sources or {}
    answerability = answerability or {}

    def route(question, kb_id):
        if question in error_on:
            raise RuntimeError("jev unavailable")
        outcome = {
            "route": routes[question],
            "sources": list(sources.get(question, ())),
            "external_calls": external_calls,
            "duration_ms": duration_ms,
        }
        if question in answerability:
            outcome["answerability"] = answerability[question]
        return outcome

    return route


def correct_routes(cases):
    routes = {case["id"]: case["expected_route"] for case in cases}
    sources = {case["id"]: list(case["expected_source_names"]) for case in cases}
    return routes, sources


def evaluate(candidate, baseline_route_fn, candidate_route_fn, cases):
    return evaluate_on_benchmark(
        candidate,
        baseline_route_fn=baseline_route_fn,
        candidate_route_fn=candidate_route_fn,
        cases=cases,
    )


# ------------------------------------------------------------------
# The verdict, the thresholds, and the report shape.
# ------------------------------------------------------------------


def test_equivalent_candidate_passes_with_zero_deltas():
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 7)]
    routes, sources = correct_routes(cases)
    baseline = route_fn(routes, sources=sources, external_calls=2, duration_ms=10.0)
    candidate = route_fn(routes, sources=sources, external_calls=2, duration_ms=9.0)

    result = evaluate(gate_candidate(), baseline, candidate, cases)

    assert isinstance(result, GateResult)
    assert result.gate == GATE_NAME == "benchmark"
    assert result.passed is True
    assert result.verdict.startswith("passed")
    stats = result.stats
    assert stats["cases"] == 6
    assert stats["route_delta_pp"] == 0.0
    assert stats["top5_delta_pp"] == 0.0
    assert stats["baseline"]["route_accuracy"] == 1.0
    assert stats["baseline"]["route_hits"] == 6
    assert stats["candidate"]["top5_hit_rate"] == 1.0
    assert stats["external_calls_delta"] == 0
    assert stats["baseline"]["external_calls"] == 12
    assert stats["baseline"]["duration_ms"] == 60.0
    assert stats["candidate"]["duration_ms"] == 54.0
    assert stats["thresholds"] == {"route_drop_max_pp": 0.0, "top5_drop_max_pp": 2.0}
    assert stats["candidate_ref"]["target"] == "preflight"
    assert stats["candidate_ref"]["questions_overridden"] == ["intent"]
    assert stats["candidate_ref"]["question_set_version"] == PREFLIGHT_QUESTION_SET_VERSION
    assert len(result.per_case) == 6
    assert set(result.per_case[0]) == PER_CASE_KEYS
    assert set(result.per_case[0]["baseline"]) == SIDE_KEYS
    assert set(stats) == STATS_KEYS
    assert set(stats["baseline"]) == SIDE_STATS_KEYS
    assert all(entry["route_changed"] is False for entry in result.per_case)
    assert all(entry["route_regressed"] is False for entry in result.per_case)


# ------------------------------------------------------------------
# 可重复性：同一候选在**同条件下**重复测量，只有稳定的判定才算数（2026-10-13 实测）。
# ------------------------------------------------------------------


def test_repeated_runs_that_agree_keep_the_verdict_and_record_every_run():
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 6)]
    routes, sources = correct_routes(cases)
    baseline = route_fn(routes, sources=sources)
    candidate = route_fn(routes, sources=sources)

    result = evaluate_repeatable_on_benchmark(
        gate_candidate(), baseline_route_fn=baseline, candidate_route_fn=candidate, cases=cases, repeats=3
    )

    assert result.passed is True and result.inconclusive is False
    assert result.stats["repeat_summary"] == "3 repeat(s): 3 passed, 0 failed"
    assert [run["passed"] for run in result.stats["repeats"]] == [True, True, True]
    assert "[3 repeat(s): 3 passed, 0 failed]" in result.verdict
    # 阈值一个字没动
    assert result.stats["thresholds"] == {"route_drop_max_pp": 0.0, "top5_drop_max_pp": 2.0}


def test_repeated_runs_that_disagree_are_inconclusive_not_a_rejection():
    """**这条就是 2026-10-13 实测的那个形状**：同一候选四次得到 passed/−10/−15/−2.3pp。

    不稳定的判定必须记为未决（候选留在 candidate_testing 等重试），而不是"拒绝"——
    否则随机性会被当成候选的退化，一条候选就被一次抛硬币判死。
    """
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 6)]
    routes, sources = correct_routes(cases)
    baseline = route_fn(routes, sources=sources)
    state = {"run": 0}

    def flaky(question, kb_id):
        perfect = state["run"] % 2 == 0
        return {
            "route": routes[question] if perfect else "agent",
            "sources": list(sources[question]) if perfect else [],
            "external_calls": 0,
            "duration_ms": 0.0,
        }

    def candidate(question, kb_id):
        if question == cases[0]["id"]:
            state["run"] += 1
        return flaky(question, kb_id)

    result = evaluate_repeatable_on_benchmark(
        gate_candidate(), baseline_route_fn=baseline, candidate_route_fn=candidate, cases=cases, repeats=3
    )

    assert result.inconclusive is True, "不一致的重复必须记为未决"
    assert result.passed is False
    assert "did not agree with itself" in result.verdict
    assert "3 repeat(s):" in result.verdict
    # 三次里两坏一好（计数器在第一条用例上自增，所以序列从"坏"开始）——重点是有好有坏
    assert [run["passed"] for run in result.stats["repeats"]] == [False, True, False]


def test_repeated_runs_are_inconclusive_when_one_run_was_inconclusive():
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    routes, sources = correct_routes(cases)
    baseline = route_fn(routes, sources=sources)
    candidate = route_fn(routes, sources=sources)
    state = {"run": 0}

    def flaky_baseline(question, kb_id):
        if question == cases[0]["id"]:
            state["run"] += 1
        if state["run"] == 2 and question == cases[0]["id"]:
            raise RuntimeError("provider timeout")
        return baseline(question, kb_id)

    result = evaluate_repeatable_on_benchmark(
        gate_candidate(), baseline_route_fn=flaky_baseline, candidate_route_fn=candidate, cases=cases, repeats=3
    )

    assert result.inconclusive is True
    assert "inconclusive runs: [2]" in result.verdict


def test_a_single_repeat_is_exactly_the_plain_evaluation():
    """``repeats=1`` 逐字节等于既有行为：既有调用方与用例不受影响。"""
    cases = [gate_case("rag-001")]
    routes, sources = correct_routes(cases)
    baseline = route_fn(routes, sources=sources)
    candidate = route_fn(routes, sources=sources)

    plain = evaluate(gate_candidate(), baseline, candidate, cases)
    repeatable = evaluate_repeatable_on_benchmark(
        gate_candidate(), baseline_route_fn=baseline, candidate_route_fn=candidate, cases=cases, repeats=1
    )

    assert repeatable == plain
    assert "repeat_summary" not in plain.stats


@pytest.mark.parametrize("repeats", [0, -1, 2, 4])
def test_repeat_counts_must_be_odd_and_positive(repeats):
    """偶数次没有多数票可谈；0 与负数没有意义。"""
    cases = [gate_case("rag-001")]
    routes, sources = correct_routes(cases)
    fn = route_fn(routes, sources=sources)

    with pytest.raises(ValueError, match="repeat"):
        evaluate_repeatable_on_benchmark(
            gate_candidate(), baseline_route_fn=fn, candidate_route_fn=fn, cases=cases, repeats=repeats
        )


def test_thresholds_are_pinned_at_the_acceptance_values():
    """A widened threshold must break this test, not a candidate."""
    assert GATE_NAME == "benchmark"
    assert ROUTE_DELTA_MAX_PP == 0.0
    assert TOP5_DELTA_MAX_PP == 2.0


def test_top5_drop_of_exactly_two_pp_still_passes():
    """One lost hit in fifty cases is exactly the 2pp budget — still a pass."""
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 51)]
    routes, sources = correct_routes(cases)
    worse_sources = {
        case["id"]: ([] if index == 1 else list(case["expected_source_names"]))
        for index, case in enumerate(cases, start=1)
    }

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources),
        route_fn(routes, sources=worse_sources),
        cases,
    )

    assert result.stats["top5_delta_pp"] == -2.0
    assert result.passed is True
    assert result.stats["top5_regressions"] == ["rag-001"]


def test_top5_drop_beyond_two_pp_fails_the_gate():
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 51)]
    routes, sources = correct_routes(cases)
    worse_sources = {
        case["id"]: ([] if index <= 5 else list(case["expected_source_names"]))
        for index, case in enumerate(cases, start=1)
    }

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources),
        route_fn(routes, sources=worse_sources),
        cases,
    )

    assert result.passed is False
    assert result.stats["route_delta_pp"] == 0.0  # routing itself stayed perfect
    assert result.stats["top5_delta_pp"] == -10.0
    assert result.stats["top5_regressions"] == [f"rag-{index:03d}" for index in range(1, 6)]
    assert "top-5 hit dropped 10.0pp" in result.verdict
    assert result.verdict.startswith("failed")


def test_candidate_with_worse_routing_is_rejected_and_marks_regression_cases():
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 9)]
    routes, sources = correct_routes(cases)
    worse = dict(routes)
    worse["rag-003"] = "structured"  # baseline right, candidate wrong
    worse["rag-004"] = "clarification"

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources),
        route_fn(worse, sources=sources),
        cases,
    )

    assert result.passed is False
    assert result.stats["route_delta_pp"] == -25.0
    assert result.stats["route_regressions"] == ["rag-003", "rag-004"]
    assert "route accuracy dropped 25.0pp" in result.verdict
    assert "rag-003" in result.verdict and "rag-004" in result.verdict
    by_case = {entry["case_id"]: entry for entry in result.per_case}
    regressed = by_case["rag-003"]
    assert regressed["route_regressed"] is True
    assert regressed["route_changed"] is True
    assert regressed["baseline"]["route"] == "rag"
    assert regressed["baseline"]["route_correct"] is True
    assert regressed["candidate"]["route"] == "structured"
    assert regressed["candidate"]["route_correct"] is False
    assert by_case["rag-001"]["route_regressed"] is False


def test_route_delta_of_exactly_zero_pp_passes_even_when_cases_change():
    """No accuracy loss overall: two lost cases and two gained cases cancel."""
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 21)]
    routes, sources = correct_routes(cases)
    baseline_routes = {
        case["id"]: ("rag" if index <= 15 else "structured") for index, case in enumerate(cases, start=1)
    }
    candidate_routes = {
        case["id"]: ("rag" if index > 5 else "structured") for index, case in enumerate(cases, start=1)
    }

    result = evaluate(
        gate_candidate(),
        route_fn(baseline_routes, sources=sources),
        route_fn(candidate_routes, sources=sources),
        cases,
    )

    assert result.stats["route_delta_pp"] == 0.0
    assert result.passed is True
    assert result.stats["route_regressions"] == [f"rag-{index:03d}" for index in range(1, 6)]
    assert result.stats["improvements"] == [f"rag-{index:03d}" for index in range(16, 21)]


# ------------------------------------------------------------------
# Failures and unlabeled cases never flatter a candidate.
# ------------------------------------------------------------------


def test_candidate_route_failure_fails_the_gate():
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    routes, sources = correct_routes(cases)

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources),
        route_fn(routes, sources=sources, error_on=("rag-002",)),
        cases,
    )

    assert result.passed is False
    assert result.stats["candidate"]["errors"] == 1
    assert "candidate route call failed" in result.verdict
    failed = {entry["case_id"]: entry for entry in result.per_case}["rag-002"]
    assert failed["candidate"]["error"] is not None
    assert "RuntimeError" in failed["candidate"]["error"]
    assert failed["candidate"]["route_correct"] is False


def test_baseline_route_failure_makes_the_gate_inconclusive():
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    routes, sources = correct_routes(cases)

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources, error_on=("rag-001",)),
        route_fn(routes, sources=sources),
        cases,
    )

    assert result.passed is False
    assert result.inconclusive is True
    assert result.stats["baseline"]["errors"] == 1
    assert "baseline route call failed" in result.verdict
    assert "inconclusive" in result.verdict


def test_both_sides_crashing_on_the_same_case_is_inconclusive():
    """两侧都被基础设施打断：比较不可信，不能记成候选退化。"""
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    routes, sources = correct_routes(cases)

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources, error_on=("rag-001", "rag-002")),
        route_fn(routes, sources=sources, error_on=("rag-001", "rag-002")),
        cases,
    )

    assert result.passed is False
    assert result.inconclusive is True


def test_candidate_specific_crash_is_not_inconclusive():
    """候选独有的崩溃是候选自己的问题：必须判失败，不给重试。"""
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    routes, sources = correct_routes(cases)

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources),
        route_fn(routes, sources=sources, error_on=("rag-002",)),
        cases,
    )

    assert result.passed is False
    assert result.inconclusive is False


def test_cases_without_expected_sources_skip_the_top5_check():
    labeled = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    unlabeled = [
        gate_case(f"structured-{index:03d}", expected_route="structured", sources=())
        for index in range(1, 5)
    ]
    cases = labeled + unlabeled
    routes, sources = correct_routes(cases)

    result = evaluate(
        gate_candidate(), route_fn(routes, sources=sources), route_fn(routes, sources=sources), cases
    )

    assert result.passed is True
    assert result.stats["baseline"]["top5_labeled"] == 4
    assert result.stats["candidate"]["top5_labeled"] == 4
    assert result.stats["top5_delta_pp"] == 0.0
    by_case = {entry["case_id"]: entry for entry in result.per_case}
    assert by_case["structured-001"]["baseline"]["top5_hit"] is None
    assert by_case["structured-001"]["candidate"]["top5_hit"] is None
    assert by_case["structured-001"]["top5_regressed"] is False


def test_a_candidate_that_turns_answers_into_refusals_fails_the_gate():
    """证据不足就拒答是对的；把能答的问题也拒掉不是。

    这条判据不看基准标签，只看两侧行为的差：基线侧给出了答案（``answerable``），候选侧同一条
    用例却拒答（``insufficient_evidence``）⇒ 失败。它是候选 #30 那类改动的护栏——候选放宽或
    收紧 ``contains_answer_evidence`` 的措辞时，最容易的"改善"就是把拒答率调上去。
    """
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 7)]
    routes, sources = correct_routes(cases)
    baseline_answerability = {case["id"]: "answerable" for case in cases}
    candidate_answerability = dict(baseline_answerability)
    candidate_answerability["rag-003"] = "insufficient_evidence"

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources, answerability=baseline_answerability),
        route_fn(routes, sources=sources, answerability=candidate_answerability),
        cases,
    )

    assert result.passed is False
    assert result.stats["refusal_escapes"] == ["rag-003"]
    assert result.stats["refusal_misses"] == []
    assert "candidate refused 1 answer(s) the baseline gave" in result.verdict
    assert "rag-003" in result.verdict
    # 路由与 Top-5 都没动：这条失败**只**由拒答护栏产生，否则测不出它自己的牙。
    assert result.stats["route_delta_pp"] == 0.0
    assert result.stats["top5_delta_pp"] == 0.0


def test_a_candidate_that_answers_more_than_the_baseline_is_recorded_not_rejected():
    """候选少拒答一次：可能正是修好了假拒答，也可能放宽过度——记录给人看，不自动判死。"""
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    routes, sources = correct_routes(cases)
    baseline_answerability = {case["id"]: "insufficient_evidence" for case in cases}
    candidate_answerability = dict(baseline_answerability)
    candidate_answerability["rag-002"] = "answerable"

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources, answerability=baseline_answerability),
        route_fn(routes, sources=sources, answerability=candidate_answerability),
        cases,
    )

    assert result.passed is True
    assert result.stats["refusal_misses"] == ["rag-002"]
    assert result.stats["refusal_escapes"] == []


def test_an_unreadable_decision_is_not_counted_as_answering():
    """没有判定可读（旧路径、或 provider 没回判定）时不能算作"作答了"。"""
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 4)]
    routes, sources = correct_routes(cases)
    baseline_answerability = {case["id"]: "answerable" for case in cases}

    result = evaluate(
        gate_candidate(),
        route_fn(routes, sources=sources, answerability=baseline_answerability),
        route_fn(routes, sources=sources),  # 候选侧完全没有判定
        cases,
    )

    # 读不到 ≠ 给出了答案：不能把它算成"拒答滥用"，也不能算成"候选更少拒答"。
    assert result.stats["refusal_escapes"] == []
    assert result.stats["refusal_misses"] == []
    assert result.passed is True


def test_evaluate_requires_a_non_empty_case_set():
    with pytest.raises(ValueError, match="at least one case"):
        evaluate(
            gate_candidate(),
            route_fn({}, sources={}),
            route_fn({}, sources={}),
            [],
        )


# ------------------------------------------------------------------
# The store round-trip: status advance, gate log, audit, no double-run.
# ------------------------------------------------------------------


def test_run_benchmark_gate_advances_and_records_the_gate_log(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    passing = store.add_candidate(member["id"], cluster["id"], gate_candidate())
    failing = store.add_candidate(member["id"], cluster["id"], gate_candidate())
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    routes, sources = correct_routes(cases)

    def candidate_route_fn_for(row):
        if row["id"] == passing:
            return route_fn(routes, sources=sources)
        worse = dict(routes)
        worse["rag-002"] = "structured"
        return route_fn(worse, sources=sources)

    results = run_benchmark_gate(
        store,
        actor_id="evolver/benchmark",
        baseline_route_fn=route_fn(routes, sources=sources),
        candidate_route_fn=route_fn(routes, sources=sources),
        candidate_route_fn_factory=candidate_route_fn_for,
        cases=cases,
        limit=10,
    )

    assert [result.passed for result in results] == [True, False]
    assert store.get_candidate(passing)["status"] == EvolutionStatus.AWAITING_VERIFICATION.value
    assert store.get_candidate(failing)["status"] == EvolutionStatus.REJECTED.value

    passed_gate = store.get_candidate(passing)["gate_log"][1]
    assert passed_gate["gate"] == "benchmark"
    assert passed_gate["result"] == "passed"
    assert "passed" in passed_gate["detail"]
    failed_gate = store.get_candidate(failing)["gate_log"][1]
    assert failed_gate["result"] == "failed"
    assert "route accuracy dropped" in failed_gate["detail"]

    assert [row["action"] for row in store.list_audit(passing)] == ["generate", "advance"]
    assert [row["action"] for row in store.list_audit(failing)] == ["generate", "reject"]
    assert store.list_audit(passing)[1]["actor_id"] == "evolver/benchmark"

    # A second round must never re-run the gate on the same candidates.
    again = run_benchmark_gate(
        store,
        actor_id="evolver/benchmark",
        baseline_route_fn=route_fn(routes, sources=sources),
        candidate_route_fn=route_fn(routes, sources=sources),
        cases=cases,
        limit=10,
    )
    assert again == []


def test_inconclusive_gate_keeps_the_candidate_retryable(tmp_path):
    """未决轮次不烧候选：保持 candidate_testing，下一轮仍会重试它。

    实测（2026-09-30）：provider 超时让两侧都在 8 个用例上崩掉，门禁把这次未决记成了
    `rejected`（终态）——一个候选被基础设施故障白白烧掉，而且看起来像候选退化。
    """
    _, store, cluster, member = seeded(tmp_path)
    candidate_id = store.add_candidate(member["id"], cluster["id"], gate_candidate())
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    routes, sources = correct_routes(cases)
    flaky = route_fn(routes, sources=sources, error_on=("rag-001",))

    first = run_benchmark_gate(
        store,
        actor_id="evolver/benchmark",
        baseline_route_fn=flaky,
        candidate_route_fn=flaky,
        cases=cases,
        limit=10,
    )

    assert [result.inconclusive for result in first] == [True]
    row = store.get_candidate(candidate_id)
    assert row["status"] == EvolutionStatus.CANDIDATE_TESTING.value
    entry = row["gate_log"][-1]
    assert entry["gate"] == "benchmark"
    assert entry["result"] == "inconclusive"
    assert "baseline route call failed" in entry["detail"]

    # 关键：未决不算门禁结果，所以下一轮仍会重试——这次两侧正常，应当通过。
    second = run_benchmark_gate(
        store,
        actor_id="evolver/benchmark",
        baseline_route_fn=route_fn(routes, sources=sources),
        candidate_route_fn=route_fn(routes, sources=sources),
        cases=cases,
        limit=10,
    )

    assert [result.passed for result in second] == [True]
    assert store.get_candidate(candidate_id)["status"] == EvolutionStatus.AWAITING_VERIFICATION.value


def test_run_benchmark_gate_rebuilds_the_candidate_from_a_stored_row(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    candidate_id = store.add_candidate(member["id"], cluster["id"], gate_candidate())
    cases = [gate_case(f"rag-{index:03d}") for index in range(1, 5)]
    routes, sources = correct_routes(cases)
    row = store.get_candidate(candidate_id)

    rebuilt = candidate_from_row(row)

    assert isinstance(rebuilt, JevQuestionSetCandidate)
    assert rebuilt.target == "preflight"
    assert rebuilt.payload == gate_candidate().payload
    assert rebuilt.rollback_ref["question_set_version"] == PREFLIGHT_QUESTION_SET_VERSION
