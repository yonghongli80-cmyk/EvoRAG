"""Tests for the repeatable diagnosis measurement runner.

The runner's value is its arithmetic, not its model calls: per-run agreement,
majority-vote agreement, per-row stability and the contract-violation rate are
what make a prompt change comparable against noise. Every test here is offline
and calls no model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.run_diagnosis_devset import (
    Case,
    CaseResult,
    load_cases,
    render_markdown,
    summarize,
)


def test_importing_the_runner_moves_the_project_root_to_the_front():
    """直接跑脚本时，站点目录里那份陈旧 config.py 不能赢。

    The editable install appends the project root after site-packages, where a
    ``config.py`` from before the evolution phase still lives (it has no
    ``evolution_db_path``), so a membership check would leave the stale copy in
    front of the real one. The module reorders instead, which this pins.
    """
    assert sys.path[0] == str(Path(__file__).resolve().parents[1])


def case(case_id: str = "case-1", expected: str = "ranking_error") -> Case:
    return Case(question=f"问题 {case_id}", kb_id="kb1", expected_category=expected, case_id=case_id)


def result(
    *,
    case_id: str = "case-1",
    expected: str = "ranking_error",
    outcomes: list[str],
    latencies: list[float] | None = None,
) -> CaseResult:
    return CaseResult(
        case=case(case_id, expected),
        outcomes=list(outcomes),
        elapsed_ms=list(latencies if latencies is not None else [100.0] * len(outcomes)),
        reproduction_reproducible=True,
    )


# ------------------------------------------------------------------
# Loading the (local, uncommitted) case file.
# ------------------------------------------------------------------


def test_load_cases_reads_the_rows_and_defaults_the_id(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            [
                {"question": "副厂配件明细", "kb_id": "kb_accessories", "expected_category": "ranking_error"},
                {
                    "case_id": "real-020",
                    "question": "帮我找副厂配件清单",
                    "kb_id": "kb_accessories",
                    "expected_category": "ranking_error",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cases = load_cases(path)

    assert [row.case_id for row in cases] == ["case-1", "real-020"]
    assert cases[0].question == "副厂配件明细"
    assert cases[0].kb_id == "kb_accessories"


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "q"},
        [{"question": "q", "kb_id": "kb1"}],
        [{"question": "q", "kb_id": "kb1", "expected_category": ""}],
        ["not-an-object"],
    ],
)
def test_load_cases_refuses_a_malformed_file(tmp_path, payload):
    """A silently skipped row would quietly shrink the measurement."""
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        load_cases(path)


# ------------------------------------------------------------------
# One case's repeats.
# ------------------------------------------------------------------


def test_a_stable_case_is_the_one_where_every_repeat_agrees():
    stable = result(outcomes=["ranking_error", "ranking_error", "ranking_error"])
    unstable = result(outcomes=["ranking_error", "undetermined", "ranking_error"])

    assert stable.stable is True
    assert stable.matched == 3
    assert unstable.stable is False
    assert unstable.matched == 2
    assert unstable.majority() == "ranking_error"


def test_a_contract_violation_is_not_a_category():
    """Invalid output is counted separately; it must not read as agreement."""
    mixed = result(
        expected="undetermined",
        outcomes=["invalid: response is not a JSON object", "undetermined", "undetermined"],
    )

    assert mixed.contract_violations == 1
    assert mixed.stable is True  # the valid repeats agree
    assert mixed.majority() == "undetermined"
    assert mixed.matched == 2

    only_violations = result(outcomes=["invalid: x", "invalid: y"])
    assert only_violations.majority() is None
    assert only_violations.stable is False
    assert only_violations.contract_violations == 2


def test_a_tied_majority_is_not_a_verdict():
    tied = result(outcomes=["ranking_error", "undetermined"])

    assert tied.majority() is None


# ------------------------------------------------------------------
# The aggregate report.
# ------------------------------------------------------------------


def test_summarize_reports_each_run_and_the_noise_band():
    results = [
        result(case_id="a", outcomes=["ranking_error", "ranking_error", "ranking_error"], latencies=[100.0, 120.0, 110.0]),
        result(case_id="b", outcomes=["undetermined", "ranking_error", "ranking_error"], latencies=[200.0, 210.0, 205.0]),
        result(case_id="c", outcomes=["invalid: x", "undetermined", "undetermined"], latencies=[300.0, 310.0, 305.0]),
    ]

    report = summarize(results, repeats=3)

    # Run 1 catches the unstable row; runs 2-3 recover the second one, and the
    # third row never matches because its valid repeats say undetermined.
    assert report["agreement_by_run"] == [1, 2, 2]
    assert report["agreement_by_run_rate"][0] == pytest.approx(1 / 3, abs=1e-4)
    assert report["agreement_majority"] == 2
    assert report["stable_rows"] == 2  # row b flips, rows a and c do not
    assert report["contract_violations"] == 1
    assert report["contract_violation_rate"] == pytest.approx(1 / 9, abs=1e-4)
    assert report["latency_p50_ms"] == pytest.approx(205.0)
    assert report["measurable"] == 3


def test_an_unmeasurable_case_is_named_not_dropped():
    """A case without an observation row shrinks the denominator, so it is listed."""
    results = [
        result(case_id="a", outcomes=["ranking_error", "ranking_error"]),
        CaseResult(case=case("missing"), error="no_observation"),
    ]

    report = summarize(results, repeats=2)

    assert report["cases"] == 2
    assert report["measurable"] == 1
    assert report["unmeasurable"] == ["missing"]
    assert report["agreement_by_run"] == [1, 1]


def test_render_markdown_names_the_unmeasurable_case():
    results = [
        result(case_id="a", outcomes=["ranking_error", "undetermined"]),
        CaseResult(case=case("missing"), error="no_observation"),
    ]

    markdown = render_markdown(summarize(results, repeats=2), results)

    assert "多数票一致率" in markdown
    assert "契约违规率" in markdown
    assert "ranking_error / undetermined" in markdown
    assert "no_observation" in markdown
