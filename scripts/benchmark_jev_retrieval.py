"""Repeatable old-versus-new preparation benchmark for the Jev retrieval path.

Compares the legacy preparation path against the Jev preparation path on the same labeled
cases, alternating run order to reduce time-of-day bias. Answer generation is not part of the
preparation acceptance numbers; optional end-to-end timings are recorded separately.

Usage:
    python scripts/benchmark_jev_retrieval.py \
        --cases benchmarks/jev_retrieval_cases.json \
        --runs 5 --warmups 1 \
        --json-out artifacts/jev-benchmark.json \
        --markdown-out artifacts/jev-benchmark.md

Secrets: the benchmark never prints credentials, question text is redacted unless
``--include-questions`` is supplied, and reports contain no chunk text.
"""

import argparse
import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Expected route labels map to the routing domain actually produced by each path.
ROUTE_EXPECTATIONS = {
    "rag": "rag",
    "structured": "structured",
    "tool": "tool",
    "clarification": "clarification",
}

_REQUIRED_CASE_FIELDS = ("id", "question", "expected_route", "kb_id")


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    question: str
    expected_route: str
    kb_id: str
    expected_source_names: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class PreparationOutcome:
    """What one preparation call produced; produced by the caller-supplied callables."""

    preparation_ms: float
    route: str
    source_names: tuple[str, ...] = ()
    rewrite_called: bool = False
    external_calls: int = 0
    end_to_end_ms: float | None = None
    failure: str | None = None


@dataclass(frozen=True)
class Sample:
    case_id: str
    path: str
    preparation_ms: float
    end_to_end_ms: float | None
    route: str
    route_correct: bool
    top1_hit: bool | None
    top5_hit: bool | None
    rewrite_called: bool
    external_calls: int
    failure: str | None


@dataclass(frozen=True)
class LatencySummary:
    count: int
    p50_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    mean_ms: float


@dataclass(frozen=True)
class PathMetrics:
    sample_count: int
    prep_p50_ms: float
    prep_p95_ms: float
    rewrite_rate: float
    route_accuracy: float
    top1_hit_rate: float
    top5_hit_rate: float
    external_calls_per_query: float
    failed_calls: int
    route_accuracy_by_category: dict[str, float] = field(default_factory=dict)
    end_to_end_p50_ms: float | None = None
    end_to_end_p95_ms: float | None = None


@dataclass(frozen=True)
class AcceptanceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class AcceptanceVerdict:
    passed: bool
    failures: tuple[str, ...]
    checks: tuple[AcceptanceCheck, ...]


@dataclass(frozen=True)
class BenchmarkReport:
    cases: int
    warmups: int
    runs_per_path: int
    sample_count: int
    old: PathMetrics
    new: PathMetrics
    verdict: AcceptanceVerdict
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "warmups": self.warmups,
            "runs_per_path": self.runs_per_path,
            "sample_count": self.sample_count,
            "old": _metrics_to_dict(self.old),
            "new": _metrics_to_dict(self.new),
            "acceptance": {
                "passed": self.verdict.passed,
                "failures": list(self.verdict.failures),
                "checks": [
                    {"name": check.name, "passed": check.passed, "detail": check.detail}
                    for check in self.verdict.checks
                ],
            },
            "failures": list(self.failures),
        }


def _metrics_to_dict(metrics: PathMetrics) -> dict[str, Any]:
    return {
        "sample_count": metrics.sample_count,
        "prep_p50_ms": metrics.prep_p50_ms,
        "prep_p95_ms": metrics.prep_p95_ms,
        "rewrite_rate": metrics.rewrite_rate,
        "route_accuracy": metrics.route_accuracy,
        "route_accuracy_by_category": dict(metrics.route_accuracy_by_category),
        "top1_hit_rate": metrics.top1_hit_rate,
        "top5_hit_rate": metrics.top5_hit_rate,
        "external_calls_per_query": metrics.external_calls_per_query,
        "failed_calls": metrics.failed_calls,
        "end_to_end_p50_ms": metrics.end_to_end_p50_ms,
        "end_to_end_p95_ms": metrics.end_to_end_p95_ms,
    }


def load_cases(path: str | Path) -> tuple[BenchmarkCase, ...]:
    """Load and validate the deterministic case set; secrets never belong in it."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = raw["cases"] if isinstance(raw, dict) else raw
    cases: list[BenchmarkCase] = []
    seen: set[str] = set()
    for entry in entries:
        for required in _REQUIRED_CASE_FIELDS:
            if not entry.get(required):
                raise ValueError(f"benchmark case missing required field: {required}")
        case_id = entry["id"]
        if case_id in seen:
            raise ValueError(f"duplicate benchmark case id: {case_id}")
        seen.add(case_id)
        if entry["expected_route"] not in ROUTE_EXPECTATIONS:
            raise ValueError(f"unknown expected_route for {case_id}: {entry['expected_route']}")
        cases.append(
            BenchmarkCase(
                id=case_id,
                question=entry["question"],
                expected_route=entry["expected_route"],
                kb_id=entry["kb_id"],
                expected_source_names=tuple(entry.get("expected_source_names") or ()),
                notes=entry.get("notes", ""),
            )
        )
    if not cases:
        raise ValueError("benchmark case set is empty")
    return tuple(cases)


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile over unsorted samples; empty input is rejected."""
    if not values:
        raise ValueError("percentile requires at least one sample")
    if not 0.0 <= p <= 1.0:
        raise ValueError("percentile p must be within [0, 1]")
    ordered = sorted(values)
    rank = min(max(math.ceil(p * len(ordered)), 1), len(ordered))
    return float(ordered[rank - 1])


def summarize(values: Sequence[float]) -> LatencySummary:
    """Summarize a latency sample list; empty input is rejected."""
    if not values:
        raise ValueError("summarize requires at least one sample")
    ordered = sorted(float(value) for value in values)
    return LatencySummary(
        count=len(ordered),
        p50_ms=percentile(ordered, 0.50),
        p95_ms=percentile(ordered, 0.95),
        min_ms=ordered[0],
        max_ms=ordered[-1],
        mean_ms=sum(ordered) / len(ordered),
    )


def _rate(hits: int, total: int) -> float:
    return (hits / total) if total else 0.0


def compute_metrics(samples: Sequence[Sample]) -> PathMetrics:
    """Aggregate one path's samples; failed calls are counted, never timed."""
    successful = [sample for sample in samples if sample.failure is None]
    preparation = [sample.preparation_ms for sample in successful]
    end_to_end = [
        sample.end_to_end_ms for sample in successful if sample.end_to_end_ms is not None
    ]
    with_sources = [sample for sample in successful if sample.top5_hit is not None]
    top1_values = [sample for sample in with_sources if sample.top1_hit is not None]

    by_category: dict[str, list[Sample]] = {}
    for sample in successful:
        by_category.setdefault(sample.case_id.split("-")[0], []).append(sample)

    if preparation:
        latency = summarize(preparation)
        p50_ms, p95_ms = latency.p50_ms, latency.p95_ms
    else:
        p50_ms = p95_ms = 0.0

    return PathMetrics(
        sample_count=len(samples),
        prep_p50_ms=p50_ms,
        prep_p95_ms=p95_ms,
        rewrite_rate=_rate(sum(1 for s in successful if s.rewrite_called), len(successful)),
        route_accuracy=_rate(sum(1 for s in successful if s.route_correct), len(successful)),
        top1_hit_rate=_rate(sum(1 for s in top1_values if s.top1_hit), len(top1_values)),
        top5_hit_rate=_rate(sum(1 for s in with_sources if s.top5_hit), len(with_sources)),
        external_calls_per_query=(
            sum(s.external_calls for s in successful) / len(successful) if successful else 0.0
        ),
        failed_calls=len(samples) - len(successful),
        route_accuracy_by_category={
            category: _rate(sum(1 for s in group if s.route_correct), len(group))
            for category, group in sorted(by_category.items())
        },
        end_to_end_p50_ms=summarize(end_to_end).p50_ms if end_to_end else None,
        end_to_end_p95_ms=summarize(end_to_end).p95_ms if end_to_end else None,
    )


def evaluate_acceptance(old: PathMetrics, new: PathMetrics) -> AcceptanceVerdict:
    """Apply the fixed acceptance thresholds; thresholds are never weakened to pass."""
    p50_target = old.prep_p50_ms * 0.70
    p95_target = old.prep_p95_ms * 0.80
    if old.rewrite_rate > 0:
        rewrite_target = old.rewrite_rate * 0.30
        rewrite_ok = new.rewrite_rate <= rewrite_target
    else:
        rewrite_target = 0.0
        rewrite_ok = new.rewrite_rate == 0.0
    top5_floor = old.top5_hit_rate - 0.02

    checks = (
        AcceptanceCheck(
            "p50",
            new.prep_p50_ms <= p50_target,
            f"prep P50 {new.prep_p50_ms:.1f}ms <= {p50_target:.1f}ms (old {old.prep_p50_ms:.1f}ms, need -30%)",
        ),
        AcceptanceCheck(
            "p95",
            new.prep_p95_ms <= p95_target,
            f"prep P95 {new.prep_p95_ms:.1f}ms <= {p95_target:.1f}ms (old {old.prep_p95_ms:.1f}ms, need -20%)",
        ),
        AcceptanceCheck(
            "rewrite",
            rewrite_ok,
            f"rewrite rate {new.rewrite_rate:.3f} <= {rewrite_target:.3f} (old {old.rewrite_rate:.3f}, need -70%)",
        ),
        AcceptanceCheck(
            "route",
            new.route_accuracy >= 0.95,
            f"route accuracy {new.route_accuracy:.3f} >= 0.95",
        ),
        AcceptanceCheck(
            "top5",
            new.top5_hit_rate >= top5_floor,
            f"Top-5 hit rate {new.top5_hit_rate:.3f} >= {top5_floor:.3f} (old {old.top5_hit_rate:.3f}, max loss 2pp)",
        ),
    )
    failures = tuple(check.name for check in checks if not check.passed)
    return AcceptanceVerdict(passed=not failures, failures=failures, checks=checks)


def _sample_from(case: BenchmarkCase, path: str, outcome: PreparationOutcome) -> Sample:
    expected_domain = ROUTE_EXPECTATIONS[case.expected_route]
    if outcome.failure is not None:
        return Sample(
            case_id=case.id,
            path=path,
            preparation_ms=0.0,
            end_to_end_ms=None,
            route="error",
            route_correct=False,
            top1_hit=None,
            top5_hit=None,
            rewrite_called=False,
            external_calls=0,
            failure=outcome.failure,
        )
    names = tuple(outcome.source_names)
    top1_hit = None
    top5_hit = None
    if case.expected_source_names:
        expected = set(case.expected_source_names)
        top1_hit = bool(names) and names[0] in expected
        top5_hit = any(name in expected for name in names[:5])
    return Sample(
        case_id=case.id,
        path=path,
        preparation_ms=outcome.preparation_ms,
        end_to_end_ms=outcome.end_to_end_ms,
        route=outcome.route,
        route_correct=outcome.route == expected_domain,
        top1_hit=top1_hit,
        top5_hit=top5_hit,
        rewrite_called=outcome.rewrite_called,
        external_calls=outcome.external_calls,
        failure=None,
    )


def run_benchmark(
    cases: Sequence[BenchmarkCase],
    old_prepare: Callable[[BenchmarkCase], PreparationOutcome],
    new_prepare: Callable[[BenchmarkCase], PreparationOutcome],
    *,
    runs: int = 5,
    warmups: int = 1,
) -> BenchmarkReport:
    """Run both preparation paths per case in alternating order and aggregate the report."""
    if runs < 1:
        raise ValueError("runs must be at least 1")
    if warmups < 0:
        raise ValueError("warmups must not be negative")

    samples: list[Sample] = []
    failures: list[str] = []

    def invoke(prepare, case):
        try:
            return prepare(case)
        except Exception as error:  # a failed call is recorded, never silently dropped
            kind = type(error).__name__
            failures.append(f"{case.id}: {kind}")
            return PreparationOutcome(preparation_ms=0.0, route="error", failure=kind)

    for case in cases:
        for _ in range(warmups):
            invoke(old_prepare, case)
            invoke(new_prepare, case)
        for run_index in range(runs):
            order = ("old", "new") if run_index % 2 == 0 else ("new", "old")
            for path in order:
                prepare = old_prepare if path == "old" else new_prepare
                samples.append(_sample_from(case, path, invoke(prepare, case)))

    old_metrics = compute_metrics([sample for sample in samples if sample.path == "old"])
    new_metrics = compute_metrics([sample for sample in samples if sample.path == "new"])
    verdict = evaluate_acceptance(old_metrics, new_metrics)
    return BenchmarkReport(
        cases=len(cases),
        warmups=warmups,
        runs_per_path=runs,
        sample_count=len(samples),
        old=old_metrics,
        new=new_metrics,
        verdict=verdict,
        failures=tuple(failures),
    )


def _escape_markdown(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("`", "\\`")


def render_markdown(
    report: BenchmarkReport,
    *,
    cases: Sequence[BenchmarkCase] = (),
    include_questions: bool = False,
) -> str:
    """Render the Markdown report; question text is redacted unless explicitly included."""
    questions = {case.id: case.question for case in cases}
    lines = [
        "# Jev Retrieval Benchmark",
        "",
        f"- cases: {report.cases}",
        f"- warmups per path: {report.warmups}",
        f"- runs per path: {report.runs_per_path}",
        f"- samples: {report.sample_count}",
        "",
        "## Preparation latency",
        "",
        "| path | P50 (ms) | P95 (ms) | end-to-end P50 (ms) |",
        "| --- | --- | --- | --- |",
        f"| old | {report.old.prep_p50_ms:.1f} | {report.old.prep_p95_ms:.1f} | {_optional(report.old.end_to_end_p50_ms)} |",
        f"| new | {report.new.prep_p50_ms:.1f} | {report.new.prep_p95_ms:.1f} | {_optional(report.new.end_to_end_p50_ms)} |",
        "",
        "## Quality and cost",
        "",
        "| path | route accuracy | Top-1 hit | Top-5 hit | rewrite rate | external calls/query | failed |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        f"| old | {report.old.route_accuracy:.3f} | {report.old.top1_hit_rate:.3f} | {report.old.top5_hit_rate:.3f} | {report.old.rewrite_rate:.3f} | {report.old.external_calls_per_query:.2f} | {report.old.failed_calls} |",
        f"| new | {report.new.route_accuracy:.3f} | {report.new.top1_hit_rate:.3f} | {report.new.top5_hit_rate:.3f} | {report.new.rewrite_rate:.3f} | {report.new.external_calls_per_query:.2f} | {report.new.failed_calls} |",
        "",
        "## Route accuracy by category (new path)",
        "",
    ]
    if report.new.route_accuracy_by_category:
        for category, accuracy in report.new.route_accuracy_by_category.items():
            lines.append(f"- {category}: {accuracy:.3f}")
    else:
        lines.append("- (no successful samples)")
    lines.extend(["", "## Acceptance", "", f"verdict: {'PASS' if report.verdict.passed else 'FAIL'}", ""])
    for check in report.verdict.checks:
        marker = "PASS" if check.passed else "FAIL"
        lines.append(f"- [{marker}] {check.name}: {check.detail}")
    if report.failures:
        lines.extend(["", "## Failed calls", ""])
        for failure in report.failures:
            lines.append(f"- {_escape_markdown(failure)}")
    if include_questions and questions:
        lines.extend(["", "## Cases", "", "| id | question |", "| --- | --- |"])
        for case_id, question in questions.items():
            lines.append(f"| {_escape_markdown(case_id)} | {_escape_markdown(question)} |")
    lines.append("")
    return "\n".join(lines)


def _optional(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def build_live_preparations(
    *,
    kb_id: str,
    memory_db_path: str = ":memory:",
) -> tuple[Callable[[BenchmarkCase], PreparationOutcome], Callable[[BenchmarkCase], PreparationOutcome]]:
    """Build in-process old/new preparation callables against one cached pipeline per KB.

    The same pipeline serves both paths: ``settings.jev_mode`` selects the legacy or the Jev
    preparation path per call, so the comparison isolates the path rather than the process.
    External-call counting wraps the real seams and still calls through to them.
    """
    from unittest.mock import patch

    import rag.agent
    import rag.pipeline
    from config import settings
    from rag.pipeline import RAGPipeline

    pipelines: dict[str, RAGPipeline] = {}

    def pipeline_for(case: BenchmarkCase) -> RAGPipeline:
        target_kb = case.kb_id or kb_id
        if target_kb not in pipelines:
            previous_mode = settings.jev_mode
            settings.jev_mode = "active"  # always construct a service so both paths can run
            try:
                pipelines[target_kb] = RAGPipeline(
                    kb_id=target_kb,
                    memory_db_path=memory_db_path,
                )
            finally:
                settings.jev_mode = previous_mode
        return pipelines[target_kb]

    def prepare(case: BenchmarkCase, *, jev_path: bool) -> PreparationOutcome:
        pipeline = pipeline_for(case)
        previous_mode = settings.jev_mode
        previous_allowlist = settings.jev_kb_allowlist
        settings.jev_mode = "active" if jev_path else "off"
        settings.jev_kb_allowlist = case.kb_id or kb_id
        counters = {"rewrite": 0, "rerank": 0, "route": 0, "jev": 0}

        original_rewrite = rag.pipeline.rewrite_query
        original_route = rag.agent.route_question
        original_rerank = pipeline.reranker.rerank
        original_jev_route = pipeline.jev.route if pipeline.jev is not None else None
        original_jev_select = pipeline.jev.select if pipeline.jev is not None else None

        def counting(original, key):
            def wrapper(*args, **kwargs):
                counters[key] += 1
                return original(*args, **kwargs)

            return wrapper

        patches = [
            patch("rag.pipeline.rewrite_query", counting(original_rewrite, "rewrite")),
            patch("rag.agent.route_question", counting(original_route, "route")),
            patch.object(pipeline.reranker, "rerank", counting(original_rerank, "rerank")),
        ]
        if original_jev_route is not None:
            patches.append(patch.object(pipeline.jev, "route", counting(original_jev_route, "jev")))
            patches.append(patch.object(pipeline.jev, "select", counting(original_jev_select, "jev")))
        try:
            for active in patches:
                active.start()
            started = time.perf_counter_ns()
            prepared, error = pipeline._prepare_context(case.question, "benchmark", None)
            preparation_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        finally:
            for active in reversed(patches):
                active.stop()
            settings.jev_mode = previous_mode
            settings.jev_kb_allowlist = previous_allowlist

        if error is not None:
            return PreparationOutcome(preparation_ms=preparation_ms, route="blocked", failure=error)
        route = _routing_domain(prepared)
        source_names = tuple(
            source["doc_name"] for source in prepared.get("sources", []) if source.get("doc_name")
        )
        return PreparationOutcome(
            preparation_ms=preparation_ms,
            route=route,
            source_names=source_names,
            rewrite_called=counters["rewrite"] > 0,
            external_calls=sum(counters.values()),
        )

    def old(case: BenchmarkCase) -> PreparationOutcome:
        return prepare(case, jev_path=False)

    def new(case: BenchmarkCase) -> PreparationOutcome:
        return prepare(case, jev_path=True)

    return old, new


def _routing_domain(prepared: dict) -> str:
    """Return the routing domain, preferring the recorded Jev route over the final kind."""
    metadata = prepared.get("decision_metadata") or {}
    jev_route = metadata.get("jev_route")
    if jev_route in ROUTE_EXPECTATIONS.values():
        return jev_route
    route = prepared.get("route", "")
    return {"rag": "rag", "structured": "structured", "agent": "tool", "fixed": "clarification"}.get(
        route, route
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark legacy versus Jev preparation paths.")
    parser.add_argument("--cases", default="benchmarks/jev_retrieval_cases.json")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--markdown-out", default=None)
    parser.add_argument("--include-questions", action="store_true")
    parser.add_argument("--kb-id", default="rag_docs")
    parser.add_argument("--memory-db-path", default=":memory:")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    old_prepare, new_prepare = build_live_preparations(
        kb_id=args.kb_id,
        memory_db_path=args.memory_db_path,
    )
    report = run_benchmark(cases, old_prepare, new_prepare, runs=args.runs, warmups=args.warmups)

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown_out:
        path = Path(args.markdown_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_markdown(report, cases=cases, include_questions=args.include_questions),
            encoding="utf-8",
        )
    print(render_markdown(report, cases=cases, include_questions=args.include_questions))
    return 0 if report.verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
