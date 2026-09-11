from pathlib import Path

from rag.spreadsheets.catalog import SpreadsheetCatalog
from rag.spreadsheets.coordinator import SpreadsheetQueryCoordinator, SpreadsheetQueryOutcome


def test_identical_authorized_query_uses_cached_outcome(monkeypatch):
    coordinator = SpreadsheetQueryCoordinator(object(), Path("."))
    calls = {"plans": 0}
    monkeypatch.setattr("rag.spreadsheets.coordinator.build_authorized_catalog", lambda *args: SpreadsheetCatalog("kb", ()))
    def plan(*args):
        calls["plans"] += 1
        from rag.spreadsheets.planner import PlanningResult, QueryIntent
        return PlanningResult(kind="unsupported", intent=QueryIntent.STRUCTURED)
    monkeypatch.setattr("rag.spreadsheets.coordinator.plan_spreadsheet_query", plan)

    first = coordinator.query("销售额合计", kb_id="kb", user={"id": 1})
    second = coordinator.query("销售额合计", kb_id="kb", user={"id": 1})

    assert first == second == SpreadsheetQueryOutcome("unsupported")
    assert calls["plans"] == 1
