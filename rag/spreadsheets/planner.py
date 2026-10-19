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
_NUMERIC_TYPES = frozenset({"integer", "decimal"})
_NUMERIC_FUNCTIONS = frozenset({"sum", "average"})


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
    metric_refs = _column_refs(metric_matches)
    if len(metric_refs) != 1:
        # The same field name exists in more than one table: column keys are only
        # unique inside one sheet, so the plan cannot name the field without a
        # sheet.  Ask instead of picking a table for the user.
        return PlanningResult(kind="clarification_required", intent=intent, clarification=Clarification(code="ambiguous_sheet", question="该字段在多个表格中存在，请选择要统计的表格", options=_sheet_names(metric_matches)))
    sheet_key, column_key = next(iter(metric_refs))
    group_refs = _column_refs(group_matches)
    if len(group_refs) > 1:
        return PlanningResult(kind="clarification_required", intent=intent, clarification=Clarification(code="ambiguous_sheet", question="分组字段在多个表格中存在，请选择要统计的表格", options=_sheet_names(group_matches)))
    if group_refs and next(iter(group_refs))[0] != sheet_key:
        # Sheets are separate tables, not partitions of one row set, so grouping
        # one sheet by a field of another sheet is not a supported join.
        return PlanningResult(kind="clarification_required", intent=intent, clarification=Clarification(code="cross_sheet_unsupported", question="暂不支持跨表格统计，请分别询问单个表格中的字段", options=_sheet_names((*metric_matches, *group_matches))))
    types = {item[2].inferred_type for item in metric_matches}
    units = {item[2].unit_hint for item in metric_matches if item[2].unit_hint}
    if len(types) != 1:
        return PlanningResult(kind="clarification_required", intent=intent, clarification=Clarification(code="incompatible_types", question="相关表格字段类型不一致", options=tuple(sorted(types))))
    if len(units) > 1:
        return PlanningResult(kind="clarification_required", intent=intent, clarification=Clarification(code="incompatible_units", question="相关表格单位不一致，请选择统一口径", options=tuple(sorted(units))))
    function = _metric_function(question)
    metric_type = next(iter(types))
    if function in _NUMERIC_FUNCTIONS and metric_type not in _NUMERIC_TYPES:
        # Summing or averaging a text field is not a different reading of the
        # question, it is an unanswerable one.  Degrading to a count would
        # silently answer something else, so ask for a usable question instead.
        return PlanningResult(
            kind="clarification_required",
            intent=intent,
            clarification=Clarification(
                code="non_numeric_metric",
                question=(
                    f"「{metric_matches[0][2].display_name}」不是数值字段，"
                    f"无法计算{'平均值' if function == 'average' else '合计'}；"
                    "可以改问该字段的数量、最大值或最小值，或指定一个数值字段。"
                ),
            ),
        )
    group_by = tuple(dict.fromkeys(item[2].column_key for item in group_matches))
    expected_fields = _expected_field_names((*metric_matches, *group_matches))
    dataset_ids = _compatible_dataset_ids(
        catalog.datasets,
        tuple(dict.fromkeys(item[0].dataset_id for item in metric_matches)),
        expected_fields,
    )
    if not dataset_ids:
        # The metric field is present but the grouping field is not: executing
        # the key in such a table would read a column the plan never matched,
        # so ask which table to use instead of answering for another one.
        return PlanningResult(
            kind="clarification_required",
            intent=intent,
            clarification=Clarification(
                code="ambiguous_sheet",
                question="分组字段只存在于部分表格中，请选择要统计的表格",
                options=_sheet_names(group_matches),
            ),
        )
    plan = StructuredQueryPlan(dataset_ids=dataset_ids, operation="aggregate", sheet_key=sheet_key, group_by=group_by, metrics=(Metric(column_key=column_key, function=function),))
    return PlanningResult(kind="planned", intent=intent, plan=plan)


def _expected_field_names(matches) -> dict[tuple[str, str], str]:
    """Map every planned ``(sheet_key, column_key)`` to the field name it matched."""
    return {
        (sheet.sheet_key, column.column_key): column.normalized_name
        for _, sheet, column in matches
    }


def _compatible_dataset_ids(datasets, candidate_ids, expected_fields) -> tuple[int, ...]:
    """Keep only datasets that store every planned field under the planned name.

    Column keys are generated per sheet, so ``c_0001`` means whatever each
    dataset put there.  A dataset that is missing a referenced field, or stores
    a different field at that key, is dropped instead of being queried for a
    column it does not have.
    """
    by_id = {dataset.dataset_id: dataset for dataset in datasets}
    compatible = []
    for dataset_id in candidate_ids:
        dataset = by_id.get(dataset_id)
        if dataset is None:
            continue
        names = {
            (sheet.sheet_key, column.column_key): column.normalized_name
            for sheet in dataset.sheets
            for column in sheet.columns
        }
        if all(names.get(key) == name for key, name in expected_fields.items()):
            compatible.append(dataset_id)
    return tuple(compatible)


def _column_refs(matches) -> set[tuple[str, str]]:
    """Return the ``(sheet_key, column_key)`` references of catalog matches."""
    return {(sheet.sheet_key, column.column_key) for _, sheet, column in matches}


def _sheet_names(matches) -> tuple[str, ...]:
    """Return the display names of the sheets a match set touches."""
    return tuple(sorted({sheet.name for _, sheet, _ in matches}))


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
