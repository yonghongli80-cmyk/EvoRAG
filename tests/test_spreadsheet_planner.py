from pathlib import Path

from rag.spreadsheets.catalog import CatalogColumn, CatalogDataset, CatalogSheet, SpreadsheetCatalog
from rag.spreadsheets.planner import QueryIntent, classify_query, plan_spreadsheet_query


def _catalog(units=("元", "元")):
    datasets = []
    for index, unit in enumerate(units, start=1):
        columns = (
            CatalogColumn("c_0001", "品类", "品类", "text"),
            CatalogColumn("c_0002", "销售额", "销售额", "decimal", unit),
        )
        datasets.append(CatalogDataset(index, index, index, f"sales-{index}.xlsx", Path(f"{index}.db"), (CatalogSheet("sheet_0001", "销量", 10, columns),)))
    return SpreadsheetCatalog("kb_sales", tuple(datasets))


def test_numeric_question_routes_to_structured():
    assert classify_query("各品类销售额合计是多少") == QueryIntent.STRUCTURED


def test_qualitative_question_routes_to_semantic():
    assert classify_query("销售下降的原因是什么") == QueryIntent.SEMANTIC


def test_compatible_tables_produce_union_sum_plan():
    result = plan_spreadsheet_query("销售额合计是多少", _catalog())
    assert result.kind == "planned"
    assert result.plan.dataset_ids == (1, 2)
    assert result.plan.metrics[0].column_key == "c_0002"
    assert result.plan.metrics[0].function == "sum"
    # Column keys are only unique inside one sheet, so the plan names its sheet.
    assert result.plan.sheet_key == "sheet_0001"


def test_each_category_maps_to_grouped_sum():
    result = plan_spreadsheet_query("各品类销售额合计是多少", _catalog())
    assert result.plan.group_by == ("c_0001",)


def test_incompatible_units_require_clarification():
    result = plan_spreadsheet_query("销售额合计是多少", _catalog(("元", "万元")))
    assert result.kind == "clarification_required"
    assert result.clarification.code == "incompatible_units"
    assert result.plan is None
