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
}
SIDE_KEYS = {
    "route",
    "route_correct",
    "sources",
    "top5_hit",
    "external_calls",
    "duration_ms",
    "error",
}
STATS_KEYS = {
    "cases",
    "baseline",
    "candidate",
    "route_delta_pp",
    "top5_delta_pp",
    "route_regressions",
    "top5_regressions",
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


def route_fn(routes, *, sources=None, error_on=(), external_calls=0, duration_ms=0.0):
    """A deterministic route callable shaped like the gate's injection contract."""
    sources = sources or {}

    def route(question, kb_id):
        if question in error_on:
            raise RuntimeError("jev unavailable")
        return {
            "route": routes[question],
            "sources": list(sources.get(question, ())),
            "external_calls": external_calls,
            "duration_ms": duration_ms,
        }

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
    assert result.stats["baseline"]["errors"] == 1
    assert "baseline route call failed" in result.verdict
    assert "inconclusive" in result.verdict


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
