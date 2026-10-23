from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from rag.spreadsheets.coordinator import SpreadsheetQueryCoordinator
from rag.spreadsheets.planner import PlanningResult, QueryIntent
from rag.spreadsheets.query import (
    Metric,
    QueryValue,
    StructuredQueryPlan,
    StructuredQueryResult,
)


def test_query_retries_after_transient_sqlite_error(monkeypatch, tmp_path):
    dataset = SimpleNamespace(dataset_id=7, version_id=3, path=tmp_path / "dataset.sqlite3")
    catalog = SimpleNamespace(datasets=(dataset,))
    metric = Metric(column_key="c_0001", function="sum")
    plan = StructuredQueryPlan(
        dataset_id=7,
        operation="aggregate",
        sheet_key="sheet_0001",
        metrics=(metric,),
    )
    planning = PlanningResult(kind="planned", intent=QueryIntent.STRUCTURED, plan=plan)
    calls = 0

    def execute(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is temporarily locked")
        return StructuredQueryResult((QueryValue(metric, "42.00"),), (), 2)

    monkeypatch.setattr("rag.spreadsheets.coordinator.build_authorized_catalog", lambda *args: catalog)
    monkeypatch.setattr("rag.spreadsheets.coordinator.plan_spreadsheet_query", lambda *args: planning)
    monkeypatch.setattr("rag.spreadsheets.coordinator.execute_multi_dataset_query", execute)
    coordinator = SpreadsheetQueryCoordinator(object(), tmp_path)
    user = {"id": 9}

    first = coordinator.query("金额合计", kb_id="kb", user=user)
    second = coordinator.query("金额合计", kb_id="kb", user=user)
    third = coordinator.query("金额合计", kb_id="kb", user=user)

    assert first.kind == "failed"
    assert first.error_code == "dataset_read_failed"
    assert second.kind == "structured_result"
    assert second.computed_facts[0].value == "42.00"
    assert third == second
    assert calls == 2
