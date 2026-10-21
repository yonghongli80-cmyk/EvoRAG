"""Task 8: benchmark statistics, acceptance thresholds, and report rendering tests."""

import json
from types import SimpleNamespace

import pytest

from scripts.benchmark_jev_retrieval import (
    BenchmarkCase,
    PathMetrics,
    PreparationOutcome,
    _routing_domain,
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
    history=(),
) -> BenchmarkCase:
    return BenchmarkCase(
        id=case_id,
        question=f"问题 {case_id}",
        expected_route=expected_route,
        kb_id="rag_docs",
        expected_source_names=tuple(expected_sources),
        category=category,
        history=tuple(history),
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


def test_acceptance_fails_when_the_preparation_path_never_ran():
    """A run that answered without preparing is not a passing benchmark.

    A knowledge-base request without a principal is answered before retrieval in
    microseconds and with no recorded failure, so both sides report identical
    metrics. Every other threshold would hold on two empty runs; the execution
    check is what stops such a report from reading as a result.
    """
    verdict = evaluate_acceptance(
        old=metrics(prep_p50=0.0, prep_p95=0.0, route_accuracy=0.15),
        new=metrics(prep_p50=0.0, prep_p95=0.0, route_accuracy=0.15),
    )

    assert verdict.passed is False
    assert "executed" in verdict.failures
    detail = next(check.detail for check in verdict.checks if check.name == "executed")
    assert "--user-id" in detail


def test_acceptance_reports_the_execution_check_when_the_path_ran():
    verdict = evaluate_acceptance(old=metrics(), new=metrics())

    detail = next(check.detail for check in verdict.checks if check.name == "executed")
    assert "executed" not in verdict.failures
    assert "1000.0ms" in detail


def test_acceptance_route_threshold_is_absolute():
    verdict = evaluate_acceptance(
        old=metrics(route_accuracy=0.99),
        new=metrics(route_accuracy=0.90),
    )
    assert verdict.passed is False
    assert "route" in verdict.failures


def test_multi_turn_advisory_checks_never_read_as_pass():
    """Regression: the multi-turn report used to print a false inequality under [PASS].

    Observed 2026-10-19: ``[PASS] p50 (advisory): prep P50 8875.3ms <= 5415.8ms`` — the measured
    value was *worse* than the target while the line said PASS, because the advisory wrapper forced
    ``passed=True`` and kept the assertion-shaped detail. Advisory criteria must be visible as
    advisory, must not enter ``failures``, and must not assert a comparison they did not satisfy.
    """
    verdict = evaluate_acceptance(
        old=metrics(prep_p50=7736.8, prep_p95=22212.7),
        new=metrics(prep_p50=8875.3, prep_p95=21781.1),
        profile="multi_turn",
    )

    advisory = {check.name: check for check in verdict.checks if check.advisory}
    assert advisory, "multi-turn profile must mark p50/p95/rewrite as advisory"
    assert advisory["p50"].detail.count("vs target") == 1
    assert "<=" not in advisory["p50"].detail
    assert advisory["p50"].passed is False, "the measured miss is still recorded for the reader"
    assert verdict.failures == (), "an advisory miss must not fail the run"


def test_single_turn_checks_are_not_marked_advisory():
    """单轮档的 p50/p95/rewrite/route/top5 都是硬判据。

    ``refusal`` 是唯一例外，而且只在**这批用例没有答案位置标签**时才是 advisory：
    没测不等于测过且通过。带标签时它是硬判据，见 test_jev_benchmark_position.py。
    """
    verdict = evaluate_acceptance(old=metrics(), new=metrics())

    hard = {check.name for check in verdict.checks if not check.advisory}
    assert hard == {"executed", "p50", "p95", "rewrite", "route", "top5"}
    refusal = next(check for check in verdict.checks if check.name == "refusal")
    assert refusal.advisory is True
    assert refusal.passed is True


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


def test_load_cases_reads_a_multi_turn_history_in_order(tmp_path):
    """多轮用例：先行轮次按时间顺序读进来，最后一轮必须是助手。"""
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "mt-001",
                        "question": "那它多少钱？",
                        "expected_route": "rag",
                        "kb_id": "kb_accessories",
                        "category": "multi_turn",
                        "history": [
                            {"role": "user", "content": "R50 电池有吗"},
                            {"role": "assistant", "content": "有，两块装"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    loaded = load_cases(path)

    assert loaded[0].category == "multi_turn"
    assert loaded[0].history == (("user", "R50 电池有吗"), ("assistant", "有，两块装"))


@pytest.mark.parametrize(
    "history, message",
    [
        ([{"role": "system", "content": "你是助手"}], "has role 'system'"),
        ([{"role": "user", "content": "  "}], "has empty content"),
        ([{"role": "user", "content": "先问一句"}], "must end with an assistant turn"),
        ("不是列表", "must be a list"),
        ([["user", "先问一句"]], r"history\[0\] for"),
    ],
)
def test_load_cases_rejects_a_malformed_history(tmp_path, history, message):
    """历史坏掉必须**当场**拒绝：否则那个用例会静默地什么都没测、却照样报一个数字。"""
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "mt-bad",
                        "question": "那它呢",
                        "expected_route": "rag",
                        "kb_id": "kb",
                        "category": "multi_turn",
                        "history": history,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_cases(path)


def test_a_single_turn_case_has_no_history_by_default():
    """旧用例文件逐字节仍然合法：没有 `history` 键就是单轮。"""
    assert case().history == ()


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


def test_run_benchmark_gives_every_invocation_its_own_session():
    """每次调用一个独立会话 —— 多轮用例靠它保证历史里没有上一次的答案。

    共用一个会话时，上一轮写进去的问答会被下一次调用当成"历史"读出来，于是同一份用例在
    第 1 次和第 2 次测的不是同一个东西。这里用"准备函数收到的会话 id 两两不同"钉住它。
    """
    seen: list[str] = []

    def prepare(item, session_id="benchmark"):
        seen.append(session_id)
        return outcome()

    cases = [case("a"), case("b")]
    run_benchmark(cases, prepare, prepare, runs=2, warmups=1)

    # 2 用例 × (1 预热 + 2 正式) × 2 条路径
    assert len(seen) == 2 * 3 * 2
    assert len(set(seen)) == len(seen), "会话 id 必须两两不同"
    assert all(item_id in " ".join(seen) for item_id in ("a", "b"))


def test_run_benchmark_still_accepts_a_single_argument_preparation():
    """既有替身（只收一个参数）不能被这次改动弄坏。"""

    def prepare(item):
        return outcome()

    report = run_benchmark([case("a")], prepare, prepare, runs=1, warmups=0)

    assert report.sample_count == 2
    assert report.old.failed_calls == 0


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


# ------------------------------------------------------------------
# 多轮：历史被喂进"这一次调用自己的会话"，而不是某一轮的上一次。
# ------------------------------------------------------------------


class _StubPipeline:
    """只提供 `prepare` 会碰到的东西：记忆、重排器、Jev、以及 `_prepare_context`。"""

    def __init__(self):
        self.memory = _StubMemory()
        self.reranker = SimpleNamespace(rerank=lambda documents, top_k=None: documents)
        self.jev = None
        self.seen: list[tuple[str, int]] = []
        is_pipeline = self

        def _prepare_context(question, session_id, doc_name, **kwargs):
            is_pipeline.seen.append((session_id, len(self.memory.get_recent_messages(session_id, 10))))
            return ({"kind": "rag", "sources": [{"doc_name": "doc.md"}]}, None)

        self._prepare_context = _prepare_context


class _StubMemory:
    def __init__(self):
        self.sessions: dict[str, list[tuple[str, str]]] = {}

    def add_message(self, session_id, role, content):
        self.sessions.setdefault(session_id, []).append((role, content))

    def get_recent_messages(self, session_id, n=10):
        return [
            SimpleNamespace(role=role, content=content)
            for role, content in self.sessions.get(session_id, [])[-n:]
        ]

    def get_summary(self, session_id):  # pragma: no cover - 多轮用例不带摘要
        return None


def _stub_live_preparations(monkeypatch, pipeline):
    """把 `build_live_preparations` 里的真实管线换成桩：只验"历史怎么喂"。"""
    import rag.agent
    import rag.pipeline

    monkeypatch.setattr(rag.pipeline, "RAGPipeline", lambda **kwargs: pipeline)
    monkeypatch.setattr(rag.pipeline, "rewrite_query", lambda question, **kwargs: question)
    monkeypatch.setattr(rag.agent, "route_question", lambda question: "rag")
    from scripts.benchmark_jev_retrieval import build_live_preparations

    return build_live_preparations(kb_id="rag_docs")


def test_a_multi_turn_case_seeds_its_own_session_and_reads_it_back(monkeypatch):
    pipeline = _StubPipeline()
    old, new = _stub_live_preparations(monkeypatch, pipeline)
    multi = case(
        "mt-001",
        category="multi_turn",
        history=(("user", "R50 电池有吗"), ("assistant", "有，两块装")),
    )

    old(multi, "session-a")
    new(multi, "session-b")

    # 两次调用各自把自己的历史写进自己的会话，且都恰好两轮
    assert pipeline.memory.sessions["session-a"] == [
        ("user", "R50 电池有吗"),
        ("assistant", "有，两块装"),
    ]
    assert pipeline.memory.sessions["session-b"] == pipeline.memory.sessions["session-a"]
    assert [seen[1] for seen in pipeline.seen] == [2, 2]


def test_a_second_call_in_a_new_session_does_not_inherit_the_first(monkeypatch):
    """换一个会话就只剩这个用例自己的历史 —— 上一次调用留下的问答不该被读成历史。"""
    pipeline = _StubPipeline()
    old, _new = _stub_live_preparations(monkeypatch, pipeline)
    first = case("mt-001", category="multi_turn", history=(("user", "A 有吗"), ("assistant", "有")))
    second = case("mt-002", category="multi_turn", history=(("user", "B 呢"), ("assistant", "也有")))

    old(first, "session-1")
    old(second, "session-2")

    assert pipeline.memory.sessions["session-1"] == [("user", "A 有吗"), ("assistant", "有")]
    assert pipeline.memory.sessions["session-2"] == [("user", "B 呢"), ("assistant", "也有")]
    assert [seen[1] for seen in pipeline.seen] == [2, 2]


def test_a_single_turn_case_still_runs_with_history_turned_off(monkeypatch):
    """单轮用例不写历史、也不打开多轮改写开关（既有 20 例的行为逐字节不变）。"""
    from config import settings

    pipeline = _StubPipeline()
    old, _new = _stub_live_preparations(monkeypatch, pipeline)
    before = settings.query_rewrite_history_turns

    old(case("rag-001"), "session-single")

    assert pipeline.memory.sessions == {}
    assert settings.query_rewrite_history_turns == before == 0


def test_fixed_refusal_is_scored_as_rag_not_clarification():
    """证据不足的拒答仍属检索链路，不能算成 clarification。"""
    prepared = {
        "route": "fixed",
        "decision": SimpleNamespace(kind="insufficient_evidence"),
        "decision_metadata": {},
    }
    assert _routing_domain(prepared) == "rag"


def test_fixed_clarification_keeps_its_domain():
    for kind in ("clarification_required", "permission_limited"):
        prepared = {
            "route": "fixed",
            "decision": SimpleNamespace(kind=kind),
            "decision_metadata": {},
        }
        assert _routing_domain(prepared) == "clarification", kind
