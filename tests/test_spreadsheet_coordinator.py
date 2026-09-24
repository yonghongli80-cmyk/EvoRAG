from pathlib import Path
from unittest.mock import patch

from rag.spreadsheets.catalog import SpreadsheetCatalog
from rag.spreadsheets.coordinator import SpreadsheetQueryCoordinator
from rag.spreadsheets.planner import Clarification, PlanningResult, QueryIntent


def test_ambiguous_numeric_question_never_executes(monkeypatch):
    coordinator = SpreadsheetQueryCoordinator(object(), Path("."))
    catalog = SpreadsheetCatalog("kb", ())
    monkeypatch.setattr("rag.spreadsheets.coordinator.build_authorized_catalog", lambda *args: catalog)
    monkeypatch.setattr("rag.spreadsheets.coordinator.plan_spreadsheet_query", lambda *args: PlanningResult(
        kind="clarification_required", intent=QueryIntent.STRUCTURED,
        clarification=Clarification(code="ambiguous_field", question="请选择字段"),
    ))
    with patch("rag.spreadsheets.coordinator.execute_multi_dataset_query") as execute:
        outcome = coordinator.query("销售额合计", kb_id="kb", user={"id": 1})
    assert outcome.kind == "clarification_required"
    assert outcome.clarification.question == "请选择字段"
    execute.assert_not_called()
