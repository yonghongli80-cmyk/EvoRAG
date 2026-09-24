"""Layer 2 of the five candidate gates: the fixed benchmark corpus (phase 4).

A candidate question set only reaches layer 3 (shadow) when it replays the same
labeled corpus the phase-2 A/B acceptance used — ``benchmarks/jev_retrieval_cases.json``
plus any confirmed real-case set — without losing quality against the live
question set. The decision is a **pure function**: the two route callables are
injected, so this module never touches the model, the network, the pipeline, or
the clock, and every threshold is pinned by the tests rather than by a caller.

Thresholds (design §13, identical to the phase-2 A/B acceptance — they may never
be weakened to let a candidate through):

* route accuracy: the candidate may not drop at all (drop ≤ 0pp);
* Top-5 source hit: the candidate may drop at most 2pp.

Report shape: :class:`GateResult` carries the aggregate ``stats`` (both sides'
hit counts and rates, the two deltas, optional cost counters), one ``per_case``
entry per benchmark case with both sides' routes, sources and the per-case
difference flags, and a one-paragraph ``verdict`` that doubles as the
``gate_log`` detail when the outcome is written back through
:meth:`~rag.evolution.candidate_store.CandidateStore.advance_candidate`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rag.evolution.candidates import JevQuestionSetCandidate
from rag.evolution.models import EvolutionStatus

#: The name this gate records in ``candidates.gate_log``; layer 3 owns "shadow".
GATE_NAME = "benchmark"

#: Route accuracy may not drop at all relative to the live question set.
ROUTE_DELTA_MAX_PP = 0.0

#: Top-5 source hit may drop by at most two percentage points.
TOP5_DELTA_MAX_PP = 2.0

# Percentage-point deltas are rounded before comparison, so a discrete corpus
# (one case in fifty is exactly 2pp) decides on the value, not on float noise.
_PP_PRECISION = 4

# A verdict names the regressed cases but stays readable for a big corpus.
_MAX_LISTED_CASES = 5

# An injected callable's failure message is recorded, truncated, never raised.
_MAX_ERROR_CHARS = 120

# What every candidate row must carry to rebuild the frozen contract.
_CANDIDATE_ROW_FIELDS = (
    "artifact_type",
    "target",
    "payload",
    "rationale",
    "expected_effect",
    "rollback_ref",
    "confidence",
)

#: ``(question, kb_id) -> {"route": str, "sources": list[str], ...}``. Optional
#: ``external_calls``/``duration_ms`` keys are recorded as cost counters.
RouteFn = Callable[[str, str], Mapping[str, Any]]


@dataclass(frozen=True)
class GateResult:
    """One candidate's layer-2 outcome; ``gate_log`` stores it as JSON.

    ``passed`` is the single gate decision. ``stats`` aggregates both sides and
    the two deltas, ``per_case`` explains the decision case by case, and
    ``verdict`` is the human paragraph used as the ``gate_log``/audit detail.
    """

    gate: str
    passed: bool
    stats: dict
    per_case: list
    verdict: str


# ------------------------------------------------------------------
# Cases and sides.
# ------------------------------------------------------------------


@dataclass(frozen=True)
class _CaseView:
    """One labeled case, normalized from a mapping or an attribute object."""

    case_id: str
    question: str
    kb_id: str
    expected_route: str
    expected_sources: tuple[str, ...]
    category: str


def _case_view(case: object) -> _CaseView:
    """Read one benchmark case; both dicts and objects (``BenchmarkCase``) work."""
    if isinstance(case, Mapping):
        case_id = case.get("id") or case.get("case_id")
        question = case.get("question")
        kb_id = case.get("kb_id")
        expected_route = case.get("expected_route")
        raw_sources = case.get("expected_source_names") or case.get("expected_sources") or ()
        category = case.get("category") or "ordinary"
    else:
        case_id = getattr(case, "id", None) or getattr(case, "case_id", None)
        question = getattr(case, "question", None)
        kb_id = getattr(case, "kb_id", None)
        expected_route = getattr(case, "expected_route", None)
        raw_sources = getattr(case, "expected_source_names", None) or ()
        category = getattr(case, "category", None) or "ordinary"
    if not case_id or not question or not kb_id or not expected_route:
        raise ValueError(f"benchmark case needs id/question/kb_id/expected_route, got {case!r}")
    return _CaseView(
        case_id=str(case_id),
        question=str(question),
        kb_id=str(kb_id),
        expected_route=str(expected_route),
        expected_sources=tuple(str(name) for name in raw_sources),
        category=str(category),
    )


@dataclass(frozen=True)
class _SideOutcome:
    """What one side answered for one case; ``error`` records a crashed call."""

    route: str
    route_correct: bool
    sources: tuple[str, ...]
    top5_hit: bool | None
    top5_labeled: bool
    error: str | None = None
    external_calls: int = 0
    duration_ms: float = 0.0


def _invalid(kind: str, *, top5_labeled: bool) -> _SideOutcome:
    """A crashed or malformed route call: recorded as an error, never raised."""
    return _SideOutcome(
        route="error",
        route_correct=False,
        sources=(),
        top5_hit=None,
        top5_labeled=top5_labeled,
        error=kind,
    )


def _run_side(route_fn: RouteFn, view: _CaseView) -> _SideOutcome:
    """Call one injected route function for one case and normalize the answer.

    A route function that raises or returns a malformed outcome is a recorded
    failure on that side, not a gate crash: the verdict names it and the gate
    refuses to pass, which is the conservative direction for a release gate.
    """
    try:
        outcome = route_fn(view.question, view.kb_id)
    except Exception as error:  # a route crash is verdict data, never a gate crash
        message = str(error).strip().replace("\n", " ")[:_MAX_ERROR_CHARS]
        return _invalid(f"{type(error).__name__}: {message}", top5_labeled=bool(view.expected_sources))
    if not isinstance(outcome, Mapping):
        return _invalid("route function did not return a mapping", top5_labeled=bool(view.expected_sources))
    route = outcome.get("route")
    sources = outcome.get("sources")
    if not isinstance(route, str) or not route:
        return _invalid("route function returned no route", top5_labeled=bool(view.expected_sources))
    names = [str(name) for name in sources] if isinstance(sources, list | tuple) else []
    top5_hit = (
        any(name in view.expected_sources for name in names[:5]) if view.expected_sources else None
    )
    calls = outcome.get("external_calls")
    duration = outcome.get("duration_ms")
    return _SideOutcome(
        route=route,
        route_correct=route == view.expected_route,
        sources=tuple(names),
        top5_hit=top5_hit,
        top5_labeled=bool(view.expected_sources),
        external_calls=int(calls) if isinstance(calls, int | float) and not isinstance(calls, bool) else 0,
        duration_ms=float(duration) if isinstance(duration, int | float) and not isinstance(duration, bool) else 0.0,
    )


@dataclass(frozen=True)
class _SideStats:
    route_hits: int
    route_accuracy: float
    top5_hits: int
    top5_labeled: int
    top5_hit_rate: float
    errors: int
    external_calls: int
    duration_ms: float


def _aggregate(outcomes: Sequence[_SideOutcome]) -> _SideStats:
    """Aggregate one side over all cases; an error is a miss, never a skip."""
    route_hits = sum(1 for outcome in outcomes if outcome.route_correct)
    labeled = [outcome for outcome in outcomes if outcome.top5_labeled]
    top5_hits = sum(1 for outcome in labeled if outcome.top5_hit)
    return _SideStats(
        route_hits=route_hits,
        route_accuracy=route_hits / len(outcomes),
        top5_hits=top5_hits,
        top5_labeled=len(labeled),
        top5_hit_rate=(top5_hits / len(labeled)) if labeled else 0.0,
        errors=sum(1 for outcome in outcomes if outcome.error is not None),
        external_calls=sum(outcome.external_calls for outcome in outcomes),
        duration_ms=round(sum(outcome.duration_ms for outcome in outcomes), 3),
    )


def _side_to_dict(outcome: _SideOutcome) -> dict:
    return {
        "route": outcome.route,
        "route_correct": outcome.route_correct,
        "sources": list(outcome.sources),
        "top5_hit": outcome.top5_hit,
        "external_calls": outcome.external_calls,
        "duration_ms": outcome.duration_ms,
        "error": outcome.error,
    }


def _stats_to_dict(stats: _SideStats) -> dict:
    return {
        "route_hits": stats.route_hits,
        "route_accuracy": stats.route_accuracy,
        "top5_hits": stats.top5_hits,
        "top5_labeled": stats.top5_labeled,
        # 0.0 when no case carries expected sources; the delta check is skipped then.
        "top5_hit_rate": stats.top5_hit_rate,
        "errors": stats.errors,
        "external_calls": stats.external_calls,
        "duration_ms": stats.duration_ms,
    }


# ------------------------------------------------------------------
# The gate itself.
# ------------------------------------------------------------------


def _pp(rate_delta: float) -> float:
    """A rate delta as percentage points, rounded for exact-threshold decisions."""
    return round(rate_delta * 100.0, _PP_PRECISION)


def _listed(case_ids: Sequence[str]) -> str:
    """Case ids for a verdict line, capped so a big regression stays readable."""
    if not case_ids:
        return "none"
    shown = ", ".join(case_ids[:_MAX_LISTED_CASES])
    hidden = len(case_ids) - _MAX_LISTED_CASES
    return f"{shown} (+{hidden} more)" if hidden > 0 else shown


def _candidate_ref(candidate: object) -> dict:
    """Identity of the gated candidate: ids overridden, version, confidence.

    Accepts the frozen :class:`JevQuestionSetCandidate` or a stored candidate
    row; missing attributes degrade to empty values so the gate still reports.
    """
    payload = getattr(candidate, "payload", None) or {}
    rollback_ref = getattr(candidate, "rollback_ref", None) or {}
    overridden = sorted(
        question_id for question_id, entry in payload.items() if isinstance(entry, Mapping) and entry
    )
    return {
        "artifact_type": getattr(candidate, "artifact_type", ""),
        "target": getattr(candidate, "target", ""),
        "questions_overridden": overridden,
        "confidence": getattr(candidate, "confidence", ""),
        "question_set_version": rollback_ref.get("question_set_version", "") if isinstance(rollback_ref, Mapping) else "",
    }


def evaluate_on_benchmark(
    candidate: object,
    *,
    baseline_route_fn: RouteFn,
    candidate_route_fn: RouteFn,
    cases: Sequence[object],
) -> GateResult:
    """Compare one candidate against the live question set on a fixed corpus.

    ``cases`` are labeled benchmark cases (mappings or ``BenchmarkCase``-style
    objects: id, question, kb_id, expected_route, optional expected_source_names
    and category). Both route callables are invoked once per case in alternating
    order (the phase-2 bias control), so the comparison isolates the question
    set rather than the time of day.

    Decision, per the acceptance thresholds:

    * ``route_delta_pp`` (candidate minus baseline route accuracy against the
      expected routes) must be ≥ ``-ROUTE_DELTA_MAX_PP``;
    * ``top5_delta_pp`` (same for the Top-5 source hit rate over the cases that
      carry expected sources) must be ≥ ``-TOP5_DELTA_MAX_PP``;
    * any crashed route call on either side fails the gate (a candidate error is
      a regression, a baseline error leaves the gate inconclusive).

    An empty corpus raises ``ValueError``: a gate that saw nothing decides
    nothing.
    """
    views = [_case_view(case) for case in cases]
    if not views:
        raise ValueError("benchmark gate needs at least one case")

    pairs: list[tuple[_SideOutcome, _SideOutcome]] = []
    for index, view in enumerate(views):
        if index % 2 == 0:
            baseline = _run_side(baseline_route_fn, view)
            candidate_side = _run_side(candidate_route_fn, view)
        else:
            candidate_side = _run_side(candidate_route_fn, view)
            baseline = _run_side(baseline_route_fn, view)
        pairs.append((baseline, candidate_side))

    baseline_stats = _aggregate([pair[0] for pair in pairs])
    candidate_stats = _aggregate([pair[1] for pair in pairs])
    route_delta_pp = _pp(candidate_stats.route_accuracy - baseline_stats.route_accuracy)
    top5_delta_pp = _pp(candidate_stats.top5_hit_rate - baseline_stats.top5_hit_rate)
    route_drop_pp = round(-route_delta_pp, _PP_PRECISION)
    top5_drop_pp = round(-top5_delta_pp, _PP_PRECISION)

    route_regressions = [
        view.case_id
        for view, (baseline, candidate_side) in zip(views, pairs, strict=True)
        if baseline.route_correct and not candidate_side.route_correct
    ]
    top5_regressions = [
        view.case_id
        for view, (baseline, candidate_side) in zip(views, pairs, strict=True)
        if baseline.top5_hit is True and candidate_side.top5_hit is not True
    ]
    improvements = [
        view.case_id
        for view, (baseline, candidate_side) in zip(views, pairs, strict=True)
        if (candidate_side.route_correct and not baseline.route_correct)
        or (candidate_side.top5_hit is True and baseline.top5_hit is not True)
    ]
    baseline_errors = [
        view.case_id for view, (baseline, _) in zip(views, pairs, strict=True) if baseline.error is not None
    ]
    candidate_errors = [
        view.case_id for view, (_, candidate_side) in zip(views, pairs, strict=True) if candidate_side.error is not None
    ]

    top5_applicable = baseline_stats.top5_labeled > 0 and candidate_stats.top5_labeled > 0
    route_ok = route_drop_pp <= ROUTE_DELTA_MAX_PP
    top5_ok = (not top5_applicable) or top5_drop_pp <= TOP5_DELTA_MAX_PP

    reasons: list[str] = []
    if not route_ok:
        reasons.append(
            f"route accuracy dropped {route_drop_pp:.1f}pp beyond {-ROUTE_DELTA_MAX_PP:.1f}pp "
            f"(regressed cases: {_listed(route_regressions)})"
        )
    if not top5_ok:
        reasons.append(
            f"top-5 hit dropped {top5_drop_pp:.1f}pp beyond {-TOP5_DELTA_MAX_PP:.1f}pp "
            f"(regressed cases: {_listed(top5_regressions)})"
        )
    if candidate_errors:
        reasons.append(
            f"candidate route call failed on {len(candidate_errors)} case(s): {_listed(candidate_errors)}"
        )
    if baseline_errors:
        reasons.append(
            f"baseline route call failed on {len(baseline_errors)} case(s): "
            f"gate inconclusive: {_listed(baseline_errors)}"
        )

    passed = not reasons
    if passed:
        verdict = (
            f"passed: route delta {route_delta_pp:+.1f}pp (limit {-ROUTE_DELTA_MAX_PP:+.1f}pp), "
            f"top-5 delta {top5_delta_pp:+.1f}pp (limit {-TOP5_DELTA_MAX_PP:+.1f}pp) "
            f"over {len(views)} cases"
        )
    else:
        verdict = "failed: " + "; ".join(reasons)

    per_case = [
        {
            "case_id": view.case_id,
            "category": view.category,
            "expected_route": view.expected_route,
            "baseline": _side_to_dict(baseline),
            "candidate": _side_to_dict(candidate_side),
            "route_changed": baseline.error is None
            and candidate_side.error is None
            and baseline.route != candidate_side.route,
            "route_regressed": baseline.route_correct and not candidate_side.route_correct,
            "route_improved": candidate_side.route_correct and not baseline.route_correct,
            "top5_regressed": baseline.top5_hit is True and candidate_side.top5_hit is not True,
            "top5_improved": candidate_side.top5_hit is True and baseline.top5_hit is not True,
        }
        for view, (baseline, candidate_side) in zip(views, pairs, strict=True)
    ]

    stats = {
        "cases": len(views),
        "baseline": _stats_to_dict(baseline_stats),
        "candidate": _stats_to_dict(candidate_stats),
        "route_delta_pp": route_delta_pp,
        "top5_delta_pp": top5_delta_pp,
        "route_regressions": route_regressions,
        "top5_regressions": top5_regressions,
        "improvements": improvements,
        "external_calls_delta": candidate_stats.external_calls - baseline_stats.external_calls,
        "thresholds": {
            "route_drop_max_pp": ROUTE_DELTA_MAX_PP,
            "top5_drop_max_pp": TOP5_DELTA_MAX_PP,
        },
        "candidate_ref": _candidate_ref(candidate),
    }
    return GateResult(gate=GATE_NAME, passed=passed, stats=stats, per_case=per_case, verdict=verdict)


# ------------------------------------------------------------------
# The store round-trip.
# ------------------------------------------------------------------


def candidate_from_row(row: Mapping[str, Any]) -> JevQuestionSetCandidate:
    """Rebuild the frozen candidate contract from one stored candidate row."""
    return JevQuestionSetCandidate(**{field: row[field] for field in _CANDIDATE_ROW_FIELDS})


def run_benchmark_gate(
    store: object,
    *,
    actor_id: str,
    baseline_route_fn: RouteFn,
    candidate_route_fn: RouteFn | None = None,
    cases: Sequence[object],
    limit: int,
    candidate_route_fn_factory: Callable[[Mapping[str, Any]], RouteFn] | None = None,
) -> list[GateResult]:
    """Run layer 2 over every candidate that never ran it; return the results.

    The runner side of the gate: it takes
    :meth:`~rag.evolution.candidate_store.CandidateStore.candidates_without_gate_result`
    for ``GATE_NAME`` (oldest first, still in ``candidate_testing`` — so a gate
    never runs twice on the same candidate), evaluates each one, and reports the
    outcome back through
    :meth:`~rag.evolution.candidate_store.CandidateStore.advance_candidate`
    (``awaiting_verification`` on pass, ``rejected`` on fail, both with
    ``gate="benchmark"`` and the verdict as the gate/audit detail).

    ``candidate_route_fn_factory(row)`` builds the per-candidate route callable,
    because each candidate overlays a different part of the question set. When
    it is omitted, the single ``candidate_route_fn`` serves every candidate —
    the shape a test or an equivalent-candidate smoke round uses; one of the two
    must be supplied.
    """
    if candidate_route_fn is None and candidate_route_fn_factory is None:
        raise ValueError("supply candidate_route_fn or candidate_route_fn_factory")
    if not cases:
        raise ValueError("benchmark gate needs at least one case")

    results: list[GateResult] = []
    for row in store.candidates_without_gate_result(GATE_NAME, limit):
        candidate = candidate_from_row(row)
        route_fn = (
            candidate_route_fn if candidate_route_fn_factory is None else candidate_route_fn_factory(row)
        )
        result = evaluate_on_benchmark(
            candidate,
            baseline_route_fn=baseline_route_fn,
            candidate_route_fn=route_fn,
            cases=cases,
        )
        # An illegal move here means another writer advanced the row between
        # the scan and the report; the store raises and the round stops rather
        # than silently skipping a candidate.
        store.advance_candidate(
            row["id"],
            EvolutionStatus.AWAITING_VERIFICATION.value if result.passed else EvolutionStatus.REJECTED.value,
            actor_id=actor_id,
            note=f"层2 基准门禁: {result.verdict}",
            gate=GATE_NAME,
            gate_detail=result.verdict,
        )
        results.append(result)
    return results


__all__ = [
    "GATE_NAME",
    "ROUTE_DELTA_MAX_PP",
    "TOP5_DELTA_MAX_PP",
    "GateResult",
    "RouteFn",
    "candidate_from_row",
    "evaluate_on_benchmark",
    "run_benchmark_gate",
]
