"""Task 8: benchmark statistics, acceptance thresholds, and report rendering tests."""

import json

import pytest

from scripts.benchmark_jev_retrieval import (
    BenchmarkCase,
    PathMetrics,
    PreparationOutcome,
    evaluate_acceptance,
    load_cases,
    percentile,
    render_markdown,
    run_benchmark,
    summarize,
)


def case(
    case_id: str = "rag-001",
    expected_sources=("doc.md",),
    expected_route: str = "rag",
    category: str = "ordinary",
) -> BenchmarkCase:
    return BenchmarkCase(
        id=case_id,
        question=f"问题 {case_id}",
        expected_route=expected_route,
        kb_id="rag_docs",
        expected_source_names=tuple(expected_sources),
        category=category,
    )


def outcome(**overrides) -> PreparationOutcome:
    values = {
        "preparation_ms": 100.0,
        "route": "rag",
        "source_names": ("doc.md",),
        "rewrite_called": False,
        "external_calls": 2,
        "end_to_end_ms": None,
        "failure": None,
    }
    values.update(overrides)
    return PreparationOutcome(**values)


def metrics(**overrides) -> PathMetrics:
    values = {
        "sample_count": 10,
        "prep_p50_ms": 1000.0,
        "prep_p95_ms": 2000.0,
        "rewrite_rate": 1.0,
        "route_accuracy": 0.99,
        "top1_hit_rate": 0.95,
        "top5_hit_rate": 0.90,
        "external_calls_per_query": 3.0,
        "failed_calls": 0,
    }
    aliases = {"prep_p50": "prep_p50_ms", "prep_p95": "prep_p95_ms", "top5": "top5_hit_rate", "top1": "top1_hit_rate"}
    for key, value in overrides.items():
        values[aliases.get(key, key)] = value
    return PathMetrics(**values)


def test_percentiles_are_computed_from_sorted_samples():
    summary = summarize([10, 20, 30, 40, 50])
    assert summary.p50_ms == 30
    assert summary.p95_ms == 50


def test_summarize_rejects_empty_samples():
    with pytest.raises(ValueError, match="at least one sample"):
        summarize([])


def test_summarize_handles_single_sample():
    summary = summarize([42.0])
    assert summary.p50_ms == 42.0
    assert summary.p95_ms == 42.0


def test_percentile_rejects_invalid_p():
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        percentile([1.0, 2.0], 1.5)


def test_failed_calls_are_counted_not_timed():
    report = run_benchmark(
        [case()],
        lambda _case: outcome(preparation_ms=100.0),
        lambda _case: outcome(preparation_ms=50.0, failure="timeout"),
        runs=2,
        warmups=0,
    )
    assert report.old.sample_count == 2
    assert report.old.failed_calls == 0
    assert report.old.prep_p50_ms == 100.0
    assert report.new.failed_calls == 2
    assert report.new.prep_p50_ms == 0.0
    assert report.new.route_accuracy == 0.0


def test_warmups_are_excluded_from_metrics():
    calls = {"old": 0, "new": 0}

    def make(path):
        def prepare(_case):
            calls[path] += 1
            return outcome(preparation_ms=10.0 + calls[path])

        return prepare

    report = run_benchmark(
        [case()], make("old"), make("new"), runs=2, warmups=1
    )
    assert calls == {"old": 3, "new": 3}  # 1 warmup + 2 measured runs each
    assert report.sample_count == 4
    assert report.warmups == 1


def test_run_order_alternates_to_reduce_time_of_day_bias():
    order = []

    def make(path):
        def prepare(_case):
            order.append(path)
            return outcome()

        return prepare

    run_benchmark([case()], make("old"), make("new"), runs=2, warmups=0)
    assert order == ["old", "new", "new", "old"]


def test_acceptance_rejects_fast_but_inaccurate_candidate():
    verdict = evaluate_acceptance(
        old=metrics(prep_p50=1000, prep_p95=2000, top5=.90),
        new=metrics(prep_p50=500, prep_p95=1000, top5=.80, rewrite_rate=0.1),
    )
    assert verdict.passed is False
    assert "top5" in verdict.failures
    assert verdict.failures == ("top5",)


def test_acceptance_passes_when_all_thresholds_hold():
    verdict = evaluate_acceptance(
        old=metrics(prep_p50=1000, prep_p95=2000, rewrite_rate=1.0, top5=.90),
        new=metrics(prep_p50=600, prep_p95=1500, rewrite_rate=0.1, route_accuracy=0.97, top5=.90),
    )
    assert verdict.passed is True
    assert verdict.failures == ()


def test_acceptance_route_threshold_is_absolute():
    verdict = evaluate_acceptance(
        old=metrics(route_accuracy=0.99),
        new=metrics(route_accuracy=0.90),
    )
    assert verdict.passed is False
    assert "route" in verdict.failures


def test_rewrite_rate_threshold_handles_zero_baseline():
    verdict = evaluate_acceptance(
        old=metrics(rewrite_rate=0.0),
        new=metrics(rewrite_rate=0.0),
    )
    assert "rewrite" not in verdict.failures
    failing = evaluate_acceptance(
        old=metrics(rewrite_rate=0.0),
        new=metrics(rewrite_rate=0.2),
    )
    assert "rewrite" in failing.failures


def test_evidence_hits_use_ranked_sources():
    report = run_benchmark(
        [case(expected_sources=("a.md", "b.md"))],
        lambda _case: outcome(source_names=("z.md", "a.md")),
        lambda _case: outcome(source_names=("a.md", "z.md")),
        runs=1,
        warmups=0,
    )
    assert report.old.top1_hit_rate == 0.0
    assert report.old.top5_hit_rate == 1.0
    assert report.new.top1_hit_rate == 1.0
    assert report.new.top5_hit_rate == 1.0


def test_cases_without_expected_sources_are_excluded_from_hit_rates():
    report = run_benchmark(
        [case(expected_sources=())],
        lambda _case: outcome(source_names=()),
        lambda _case: outcome(source_names=()),
        runs=1,
        warmups=0,
    )
    assert report.old.top5_hit_rate == 0.0
    assert report.old.sample_count == 1


def test_load_cases_validates_schema(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [{"question": "q", "expected_route": "rag", "kb_id": "kb"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required field: id"):
        load_cases(path)

    path.write_text(
        json.dumps(
            {
                "cases": [
                    {"id": "a", "question": "q", "expected_route": "rag", "kb_id": "kb"},
                    {"id": "a", "question": "q", "expected_route": "rag", "kb_id": "kb"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate benchmark case id"):
        load_cases(path)


def test_load_cases_rejects_unknown_route(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps({"cases": [{"id": "a", "question": "q", "expected_route": "magic", "kb_id": "kb"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown expected_route"):
        load_cases(path)


def test_load_cases_rejects_unknown_category(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "a",
                        "question": "q",
                        "expected_route": "rag",
                        "kb_id": "kb",
                        "category": "weird",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown category"):
        load_cases(path)


def test_rewrite_acceptance_uses_ordinary_population_only():
    """The rewrite criterion targets ordinary questions; special cases stay reported."""

    def old_prepare(_case):
        return outcome(rewrite_called=True, preparation_ms=1000.0)

    def new_prepare(case):
        return outcome(rewrite_called=case.category != "ordinary", preparation_ms=500.0)

    report = run_benchmark(
        [case("rag-001"), case("tool-001", category="special")],
        old_prepare,
        new_prepare,
        runs=1,
        warmups=0,
    )
    assert report.old.rewrite_rate == 1.0
    assert report.old.ordinary_rewrite_rate == 1.0
    assert report.new.rewrite_rate == 0.5
    assert report.new.ordinary_rewrite_rate == 0.0  # the only ordinary case never rewrote
    assert report.new.ordinary_sample_count == 1
    rewrite_check = next(check for check in report.verdict.checks if check.name == "rewrite")
    assert rewrite_check.passed is True


def test_case_breakdown_reports_each_path():
    report = run_benchmark(
        [case("rag-001", category="ordinary")],
        lambda _case: outcome(preparation_ms=100.0, external_calls=3),
        lambda _case: outcome(preparation_ms=50.0, external_calls=2),
        runs=2,
        warmups=0,
    )
    assert len(report.case_breakdowns) == 1
    item = report.case_breakdowns[0]
    assert item.case_id == "rag-001"
    assert item.old.run_count == 2
    assert item.old.route_correct_count == 2
    assert item.old.external_calls_total == 6
    assert item.new.prep_p50_ms == 50.0


def test_committed_case_set_is_deterministic_and_labeled():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "benchmarks" / "jev_retrieval_cases.json")
    assert len(cases) >= 20
    assert len({c.id for c in cases}) == len(cases)
    for item in cases:
        assert item.expected_route
        assert item.kb_id
    retrieval = [c for c in cases if c.expected_route == "rag"]
    assert retrieval
    for item in retrieval:
        labeled = (
            item.expected_source_names
            or any(tag in item.notes for tag in ("无答案", "冲突", "注入"))
        )
        assert labeled, item.id


def test_markdown_report_redacts_questions_by_default():
    report = run_benchmark(
        [case()],
        lambda _case: outcome(),
        lambda _case: outcome(),
        runs=1,
        warmups=0,
    )
    markdown = render_markdown(report, cases=[case()])
    assert "问题 rag-001" not in markdown
    assert "verdict:" in markdown
    assert "P50 (ms)" in markdown


def test_markdown_report_escapes_pipes_and_newlines():
    sneaky = BenchmarkCase(
        id="rag|002",
        question="第一行\n第二行 | 带管道 `代码`",
        expected_route="rag",
        kb_id="rag_docs",
        expected_source_names=("doc.md",),
    )
    report = run_benchmark(
        [sneaky],
        lambda _case: outcome(source_names=("doc.md",)),
        lambda _case: outcome(source_names=("doc.md",)),
        runs=1,
        warmups=0,
    )
    markdown = render_markdown(report, cases=[sneaky], include_questions=True)
    table_rows = [line for line in markdown.splitlines() if line.startswith("| rag")]
    assert table_rows
    assert "\n" not in table_rows[0]
    assert "\\|" in table_rows[0]


def test_report_json_round_trip_without_questions():
    report = run_benchmark(
        [case()],
        lambda _case: outcome(),
        lambda _case: outcome(),
        runs=1,
        warmups=0,
    )
    payload = report.to_dict()
    assert payload["cases"] == 1
    assert payload["sample_count"] == 2
    assert "问题 rag-001" not in json.dumps(payload, ensure_ascii=False)
    assert payload["acceptance"]["passed"] in {True, False}
