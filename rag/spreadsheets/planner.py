"""Safe natural-language planning for spreadsheet questions."""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rag.spreadsheets.catalog import SpreadsheetCatalog
from rag.spreadsheets.query import Metric, StructuredQueryPlan


class QueryIntent(StrEnum):
    STRUCTURED = "structured"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class Clarification(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    question: str
    options: tuple[str, ...] = ()


class PlanningResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    kind: Literal["planned", "clarification_required", "unsupported"]
    intent: QueryIntent
    plan: StructuredQueryPlan | None = None
    clarification: Clarification | None = None


_NUMERIC_CUES = ("多少", "合计", "总计", "求和", "平均", "最大", "最小", "最高", "最低", "前", "top", "数量", "个数", "统计")
_QUALITATIVE_CUES = ("原因", "为什么", "建议", "总结", "分析")


def classify_query(question: str) -> QueryIntent:
    normalized = question.strip().casefold()
    numeric = any(cue in normalized for cue in _NUMERIC_CUES)
    qualitative = any(cue in normalized for cue in _QUALITATIVE_CUES)
    if numeric and qualitative:
        return QueryIntent.HYBRID
    return QueryIntent.STRUCTURED if numeric else QueryIntent.SEMANTIC


def plan_spreadsheet_query(question: str, catalog: SpreadsheetCatalog) -> PlanningResult:
    intent = classify_query(question)
    if intent == QueryIntent.SEMANTIC:
        return PlanningResult(kind="unsupported", intent=intent)
    matches = []
    for dataset in catalog.datasets:
        for sheet in dataset.sheets:
            for column in sheet.columns:
                if column.display_name.casefold() in question.casefold() or column.normalized_name.casefold() in question.casefold():
                    matches.append((dataset, sheet, column))
    if not matches:
        return PlanningResult(kind="unsupported", intent=intent)
    numeric_matches = [item for item in matches if item[2].inferred_type in {"integer", "decimal"}]
    group_matches = [item for item in matches if f"各{item[2].display_name}" in question]
    metric_matches = numeric_matches or [item for item in matches if item not in group_matches]
    names = {item[2].normalized_name for item in metric_matches}
    if len(names) != 1:
        return PlanningResult(kind="clarification_required", intent=intent, clarification=Clarification(code="ambiguous_field", question="请选择要计算的字段", options=tuple(sorted({item[2].display_name for item in matches}))))
    types = {item[2].inferred_type for item in metric_matches}
    units = {item[2].unit_hint for item in metric_matches if item[2].unit_hint}
    if len(types) != 1:
        return PlanningResult(kind="clarification_required", intent=intent, clarification=Clarification(code="incompatible_types", question="相关表格字段类型不一致", options=tuple(sorted(types))))
    if len(units) > 1:
        return PlanningResult(kind="clarification_required", intent=intent, clarification=Clarification(code="incompatible_units", question="相关表格单位不一致，请选择统一口径", options=tuple(sorted(units))))
    function = _metric_function(question)
    column_key = metric_matches[0][2].column_key
    dataset_ids = tuple(dict.fromkeys(item[0].dataset_id for item in metric_matches))
    group_by = tuple(dict.fromkeys(item[2].column_key for item in group_matches))
    plan = StructuredQueryPlan(dataset_ids=dataset_ids, operation="aggregate", group_by=group_by, metrics=(Metric(column_key=column_key, function=function),))
    return PlanningResult(kind="planned", intent=intent, plan=plan)


def _metric_function(question: str) -> str:
    value = question.casefold()
    if "平均" in value:
        return "average"
    if "最大" in value or "最高" in value:
        return "max"
    if "最小" in value or "最低" in value:
        return "min"
    if "去重" in value:
        return "distinct_count"
    if "数量" in value or "个数" in value:
        return "count"
    return "sum"
