"""Whole-table aggregation, text-metric clarification, and failure containment.

Defect A: aggregates were computed in Python over the first ``limit`` rows
(``ORDER BY __row_number LIMIT ?``), so every table longer than the plan limit
was answered from a prefix of itself.  These tests pin the fixed behaviour:
each aggregate is evaluated by SQLite over the whole filtered table,
``rows_scanned`` reports the rows that actually took part, and the value a
truncated prefix would have produced is asserted to be different.

Defect B: a text column planned as ``sum`` raised inside the executor and
escaped to the API.  The planner now asks a usable question instead, and the
coordinator contains every other execution error as an audited failure.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from rag.pipeline import RAGPipeline
from rag.spreadsheets.catalog import (
    CatalogColumn,
    CatalogDataset,
    CatalogSheet,
    SpreadsheetCatalog,
)
from rag.spreadsheets.coordinator import SpreadsheetQueryCoordinator
from rag.spreadsheets.migration import dataset_schema_mirror
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.planner import plan_spreadsheet_query
from rag.spreadsheets.query import (
    FilterClause,
    Metric,
    QueryScope,
    StructuredQueryInputError,
    StructuredQueryPlan,
    execute_multi_dataset_query,
    execute_structured_query,
)
from rag.spreadsheets.store import read_dataset_schema, resolve_dataset_path, write_dataset
from rag.user_db import UserDB

KB_ID = "kb_integrity"
ROW_COUNT = 150
PREFIX = 100
HEADER = ["排序", "型号", "金额", "分类"]
BIG_SHEET = "sheet_0001"

EXECUTORS = (execute_structured_query, execute_multi_dataset_query)


def _rows(count: int = ROW_COUNT):
    """Rows whose first ``PREFIX`` entries sort below every later entry."""
    return [
        [
            index,
            f"{'A' if index <= PREFIX else 'Z'}-{index:04d}",
            index + 0.5,
            "前段" if index <= PREFIX else "后段",
        ]
        for index in range(1, count + 1)
    ]


def _write_workbook(path: Path, headers, rows, extra: tuple[str, list[list]] | None = None) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.title = "明细"
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    if extra is not None:
        other = book.create_sheet(extra[0])
        other.append(["品牌", "数量"])
        for row in extra[1]:
            other.append(list(row))
    book.save(path)
    return path


def _source_workbook(tmp_path: Path, name: str, headers, rows) -> Path:
    return _write_workbook(
        tmp_path / name, headers, rows, extra=("备注", [["腾龙", 2], ["适马", 1]])
    )


def _dataset_file(tmp_path: Path, name: str, headers, rows, document_id: int, version_id: int):
    source = _source_workbook(tmp_path, name, headers, rows)
    path = resolve_dataset_path(tmp_path / "datasets", document_id, version_id)
    write_dataset(path, parse_spreadsheet(source, "xlsx", None))
    return path


@pytest.fixture
def big_dataset(tmp_path):
    """A real 150-row dataset: more rows than any plan limit."""
    return _dataset_file(tmp_path, "big.xlsx", HEADER, _rows(), 1, 1)


@pytest.fixture
def big_environment(tmp_path):
    """The same dataset wired through a real users.db, as production does."""
    db = UserDB(str(tmp_path / "users.sqlite3"))
    root = tmp_path / "datasets"
    owner_id = db.create_user("owner", "Password-123!")
    owner = db.get_user_by_id(owner_id)
    document_id = db.create_document_permission("big.xlsx", KB_ID, owner_id)
    source = _source_workbook(tmp_path, "big.xlsx", HEADER, _rows())
    version_id = db.create_document_version(
        document_id, "big.xlsx", str(source), "hash", owner_id
    )
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    path = resolve_dataset_path(root, document_id, version_id)
    write_dataset(path, parse_spreadsheet(source, "xlsx", None))
    db.replace_structured_dataset_schema(dataset_id, dataset_schema_mirror(path))
    db.activate_document_and_dataset(
        document_id=document_id,
        version_id=version_id,
        dataset_id=dataset_id,
        kb_id=KB_ID,
        filename="big.xlsx",
    )
    yield db, root, owner
    db.close()


def _scope(path, dataset_id: int = 1):
    return QueryScope({dataset_id: path}, frozenset({dataset_id}))


def _plan(function: str, column_key: str = "c_0001", dataset_id: int = 1, **kwargs):
    return StructuredQueryPlan(
        dataset_id=dataset_id,
        operation="aggregate",
        sheet_key=BIG_SHEET,
        metrics=(Metric(column_key=column_key, function=function),),
        **kwargs,
    )


def _direct_sql(path, sql: str, parameters=()):
    connection = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def _query_runs(db_path):
    return _direct_sql(
        db_path,
        "SELECT status, error_code, rows_scanned FROM structured_query_runs ORDER BY id",
    )


def _catalog(datasets) -> SpreadsheetCatalog:
    """Catalog view of dataset files, exactly as the users.db mirror exposes it."""
    return SpreadsheetCatalog(
        KB_ID,
        tuple(
            CatalogDataset(
                dataset_id,
                dataset_id,
                dataset_id,
                Path(path).name,
                Path(path),
                tuple(
                    CatalogSheet(
                        sheet.sheet_key,
                        sheet.name,
                        sheet.row_count,
                        tuple(
                            CatalogColumn(
                                column.column_key,
                                column.display_name,
                                column.normalized_name,
                                column.inferred_type,
                            )
                            for column in sheet.columns
                        ),
                    )
                    for sheet in read_dataset_schema(path)
                ),
            )
            for dataset_id, path in datasets
        ),
    )


@pytest.mark.parametrize("execute", EXECUTORS)
def test_sum_and_count_cover_the_whole_table(big_dataset, execute):
    """A 150-row table must not be answered from its first 100 rows."""
    rows = _rows()
    whole_sum = sum(row[0] for row in rows)
    prefix_sum = sum(row[0] for row in rows[:PREFIX])
    direct_sum, direct_count = _direct_sql(
        big_dataset, f'SELECT SUM("c_0001"), COUNT("c_0001") FROM "{BIG_SHEET}"'
    )[0]

    result = execute(_plan("sum", "c_0001"), _scope(big_dataset))
    counted = execute(_plan("count", "c_0001"), _scope(big_dataset))

    assert result.values[0].canonical == str(whole_sum) == str(direct_sum)
    assert counted.values[0].canonical == str(direct_count) == str(len(rows))
    assert prefix_sum != whole_sum  # the truncating computation answered differently
    assert result.rows_scanned == counted.rows_scanned == len(rows)


@pytest.mark.parametrize("execute", EXECUTORS)
def test_max_is_not_bounded_by_the_plan_limit(big_dataset, execute):
    direct_max = _direct_sql(big_dataset, f'SELECT MAX("c_0002") FROM "{BIG_SHEET}"')[0][0]
    prefix_max = _direct_sql(
        big_dataset,
        f'SELECT MAX(v) FROM (SELECT "c_0002" AS v FROM "{BIG_SHEET}" '
        f"ORDER BY __row_number LIMIT {PREFIX})",
    )[0][0]

    result = execute(_plan("max", "c_0002"), _scope(big_dataset))

    assert result.values[0].canonical == direct_max == "Z-0150"
    assert prefix_max == "A-0100" != direct_max
    assert result.rows_scanned == ROW_COUNT


def test_grouped_aggregate_covers_every_row(big_dataset):
    """Grouping is a multi-dataset operation; its rows must not be truncated."""
    rows = _rows()
    plan = StructuredQueryPlan(
        dataset_id=1,
        operation="aggregate",
        sheet_key=BIG_SHEET,
        group_by=("c_0004",),
        metrics=(Metric(column_key="c_0001", function="sum"),),
    )
    direct = dict(
        _direct_sql(big_dataset, f'SELECT "c_0004", SUM("c_0001") FROM "{BIG_SHEET}" GROUP BY "c_0004"')
    )

    result = execute_multi_dataset_query(plan, _scope(big_dataset))

    assert {row.values["c_0004"]: row.values["c_0001_sum"] for row in result.rows} == {
        key: str(value) for key, value in direct.items()
    }
    assert sum(int(row.values["c_0001_sum"]) for row in result.rows) == sum(row[0] for row in rows)
    assert result.rows_scanned == len(rows)


@pytest.mark.parametrize("execute", EXECUTORS)
def test_filters_are_applied_to_the_pushed_down_aggregate(big_dataset, execute):
    rows = _rows()
    plan = _plan(
        "sum", "c_0001", filters=(FilterClause(column_key="c_0002", operator="gte", value="Z-0000"),)
    )
    direct = _direct_sql(
        big_dataset, f'SELECT SUM("c_0001") FROM "{BIG_SHEET}" WHERE "c_0002" >= ?', ("Z-0000",)
    )[0][0]

    result = execute(plan, _scope(big_dataset))

    assert result.values[0].canonical == str(direct) == str(sum(row[0] for row in rows[PREFIX:]))
    assert result.rows_scanned == ROW_COUNT - PREFIX


@pytest.mark.parametrize("execute", EXECUTORS)
def test_sheet_scope_survives_the_pushdown(big_dataset, execute):
    small = StructuredQueryPlan(
        dataset_id=1,
        operation="aggregate",
        sheet_key="sheet_0002",
        metrics=(Metric(column_key="c_0002", function="sum"),),
    )

    result = execute(small, _scope(big_dataset))

    assert result.values[0].canonical == "3"
    assert result.rows_scanned == 2
    assert result.citations[0].sheet_key == "sheet_0002"


def test_decimal_sum_and_average_are_exact_over_the_whole_table(tmp_path):
    """SQLite has no decimal type: pushdown must not buy precision with it."""
    rows = [[f"项目-{index:03d}", 0.1 if index % 2 == 0 else 0.2] for index in range(120)]
    path = _dataset_file(tmp_path, "decimal.xlsx", ["项目", "金额"], rows, 2, 2)
    scope = _scope(path, dataset_id=2)

    total = execute_structured_query(_plan("sum", "c_0002", dataset_id=2), scope)
    average = execute_structured_query(_plan("average", "c_0002", dataset_id=2), scope)

    assert total.values[0].canonical == "18.0"
    assert average.values[0].canonical == "0.15"
    assert total.rows_scanned == average.rows_scanned == 120
    prefix = sum((Decimal("0.1") if index % 2 == 0 else Decimal("0.2")) for index in range(PREFIX))
    assert format(prefix, "f") == "15.0" != total.values[0].canonical


def test_decimal_merge_across_datasets_stays_exact(tmp_path):
    first = _dataset_file(tmp_path, "one.xlsx", ["品类", "销售额"], [["相机", 0.1]], 3, 3)
    second = _dataset_file(tmp_path, "two.xlsx", ["品类", "销售额"], [["镜头", 0.2], ["镜头", 0.3]], 4, 4)
    plan = StructuredQueryPlan(
        dataset_ids=(3, 4), operation="aggregate", sheet_key=BIG_SHEET,
        metrics=(Metric(column_key="c_0002", function="sum"),),
    )

    result = execute_multi_dataset_query(plan, QueryScope({3: first, 4: second}, frozenset({3, 4})))

    assert result.values[0].canonical == "0.6"
    assert result.rows_scanned == 3


def test_distinct_count_unions_datasets_instead_of_adding(tmp_path):
    first = _dataset_file(tmp_path, "one.xlsx", ["品类", "销售额"], [["相机", 1], ["镜头", 2]], 7, 7)
    second = _dataset_file(tmp_path, "two.xlsx", ["品类", "销售额"], [["相机", 3]], 8, 8)
    plan = StructuredQueryPlan(
        dataset_ids=(7, 8), operation="aggregate", sheet_key=BIG_SHEET,
        metrics=(Metric(column_key="c_0001", function="distinct_count"),),
    )

    result = execute_multi_dataset_query(plan, QueryScope({7: first, 8: second}, frozenset({7, 8})))

    assert result.values[0].canonical == "2"  # 相机/镜头, never 2 + 1
    assert result.rows_scanned == 3


def test_average_across_datasets_weights_every_row(tmp_path):
    first = _dataset_file(tmp_path, "one.xlsx", ["品类", "销售额"], [["A", 1], ["B", 3]], 9, 9)
    second = _dataset_file(tmp_path, "two.xlsx", ["品类", "销售额"], [["C", 3]], 10, 10)
    plan = StructuredQueryPlan(
        dataset_ids=(9, 10), operation="aggregate", sheet_key=BIG_SHEET,
        metrics=(Metric(column_key="c_0002", function="average"),),
    )

    result = execute_multi_dataset_query(plan, QueryScope({9: first, 10: second}, frozenset({9, 10})))

    assert result.values[0].canonical == format(Decimal(7) / 3, "f")
    assert result.values[0].canonical != "2.5"  # not the mean of per-dataset means


def test_min_across_datasets_compares_numbers_not_text(tmp_path):
    first = _dataset_file(tmp_path, "one.xlsx", ["品类", "销售额"], [["A", 9]], 11, 11)
    second = _dataset_file(tmp_path, "two.xlsx", ["品类", "销售额"], [["B", 12]], 12, 12)
    plan = StructuredQueryPlan(
        dataset_ids=(11, 12), operation="aggregate", sheet_key=BIG_SHEET,
        metrics=(Metric(column_key="c_0002", function="min"),),
    )

    result = execute_multi_dataset_query(plan, QueryScope({11: first, 12: second}, frozenset({11, 12})))

    assert result.values[0].canonical == "9"  # text comparison would answer "12"


def test_count_ignores_null_cells_but_reports_every_scanned_row(tmp_path):
    rows = [["A", 1], ["B", None], ["C", 3]]
    path = _dataset_file(tmp_path, "nulls.xlsx", ["品类", "销售额"], rows, 13, 13)

    count = execute_structured_query(_plan("count", "c_0002", dataset_id=13), _scope(path, 13))

    assert count.values[0].canonical == "2"
    assert count.rows_scanned == 3


def test_extremes_ignore_datasets_with_no_matching_row(tmp_path):
    """An empty partial must not win a min/max by being the smallest string."""
    first = _dataset_file(tmp_path, "one.xlsx", ["品类", "销售额"], [["A", 5], ["B", 7]], 14, 14)
    second = _dataset_file(tmp_path, "two.xlsx", ["品类", "销售额"], [["C", 3]], 15, 15)
    plan = StructuredQueryPlan(
        dataset_ids=(14, 15), operation="aggregate", sheet_key=BIG_SHEET,
        metrics=(Metric(column_key="c_0002", function="min"),),
        filters=(FilterClause(column_key="c_0001", operator="eq", value="A"),),
    )

    result = execute_multi_dataset_query(plan, QueryScope({14: first, 15: second}, frozenset({14, 15})))

    assert result.values[0].canonical == "5"
    assert result.rows_scanned == 1


def test_numeric_aggregation_on_a_text_column_is_an_input_error(big_dataset):
    with pytest.raises(StructuredQueryInputError) as error:
        execute_structured_query(_plan("sum", "c_0002"), _scope(big_dataset))

    assert error.value.code == "non_numeric_metric"
    assert str(error.value) == "numeric aggregation requires numeric column"
    assert isinstance(error.value, ValueError)  # existing ValueError callers keep working


def test_planner_asks_instead_of_summing_a_text_column(big_dataset):
    planning = plan_spreadsheet_query("型号合计是多少？", _catalog(((1, big_dataset),)))

    assert planning.kind == "clarification_required"
    assert planning.plan is None
    assert planning.clarification is not None
    assert planning.clarification.code == "non_numeric_metric"
    assert "型号" in planning.clarification.question


def test_planner_still_plans_extremes_of_a_text_column(big_dataset):
    planning = plan_spreadsheet_query("型号最大是多少？", _catalog(((1, big_dataset),)))

    assert planning.kind == "planned"
    assert planning.plan is not None
    assert planning.plan.metrics[0].column_key == "c_0002"
    assert planning.plan.metrics[0].function == "max"


def test_planner_plans_the_sum_of_a_decimal_column(big_dataset):
    planning = plan_spreadsheet_query("金额合计是多少？", _catalog(((1, big_dataset),)))

    assert planning.kind == "planned"
    assert planning.plan is not None
    assert planning.plan.metrics[0].column_key == "c_0003"
    assert planning.plan.metrics[0].function == "sum"


def test_grouping_field_is_validated_per_dataset(tmp_path):
    """A dataset without the grouping field must not be queried for the key."""
    sales = _dataset_file(
        tmp_path, "sales.xlsx", ["品类", "销售额"], [["相机", 30], ["镜头", 20]], 5, 5
    )
    lenses = _dataset_file(
        tmp_path, "lenses.xlsx", ["型号", "销售额"], [["HA046", 10]], 6, 6
    )
    assert read_dataset_schema(lenses)[0].columns[0].display_name == "型号"
    catalog = _catalog(((5, sales), (6, lenses)))

    planning = plan_spreadsheet_query("各品类销售额合计是多少？", catalog)

    assert planning.kind == "planned"
    assert planning.plan is not None
    assert planning.plan.group_by == ("c_0001",)
    assert planning.plan.dataset_ids == (5,)

    result = execute_multi_dataset_query(
        planning.plan, QueryScope({5: sales, 6: lenses}, frozenset({5, 6}))
    )

    assert [row.values["c_0001"] for row in result.rows] == ["相机", "镜头"]
    assert [row.values["c_0002_sum"] for row in result.rows] == ["30", "20"]


def test_text_sum_is_clarified_end_to_end(big_environment):
    db, root, owner = big_environment
    coordinator = SpreadsheetQueryCoordinator(db, root)

    outcome = coordinator.query("型号合计是多少？", kb_id=KB_ID, user=owner)

    assert outcome.kind == "clarification_required"
    assert outcome.clarification is not None
    assert outcome.clarification.code == "non_numeric_metric"
    assert outcome.computed_facts == ()
    assert outcome.rows_scanned == 0
    assert _query_runs(db.db_path) == [("clarification_required", "non_numeric_metric", 0)]


def test_execution_failure_is_contained_and_audited(big_environment, monkeypatch):
    db, root, owner = big_environment

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr("rag.spreadsheets.coordinator.execute_multi_dataset_query", explode)

    outcome = SpreadsheetQueryCoordinator(db, root).query(
        "金额合计是多少？", kb_id=KB_ID, user=owner
    )

    assert outcome.kind == "failed"
    assert outcome.error_code == "dataset_read_failed"
    assert outcome.computed_facts == ()
    assert outcome.rows_scanned == 0
    assert _query_runs(db.db_path) == [("failed", "dataset_read_failed", 0)]


def test_execution_input_error_is_clarified_and_audited(big_environment, monkeypatch):
    db, root, owner = big_environment

    def reject(*args, **kwargs):
        raise StructuredQueryInputError("unknown_field", "unknown or ambiguous column key")

    monkeypatch.setattr("rag.spreadsheets.coordinator.execute_multi_dataset_query", reject)

    outcome = SpreadsheetQueryCoordinator(db, root).query(
        "金额合计是多少？", kb_id=KB_ID, user=owner
    )

    assert outcome.kind == "clarification_required"
    assert outcome.clarification is not None
    assert outcome.clarification.code == "unknown_field"
    assert _query_runs(db.db_path) == [("clarification_required", "unknown_field", 0)]


def test_pipeline_boundary_never_sees_the_execution_error(big_environment, monkeypatch):
    """The API-facing seam returns no structured route instead of raising."""
    db, root, owner = big_environment

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr("rag.spreadsheets.coordinator.execute_multi_dataset_query", explode)
    pipeline = object.__new__(RAGPipeline)

    prepared = pipeline._attempt_structured_query(
        "金额合计是多少？", SpreadsheetQueryCoordinator(db, root), owner, KB_ID
    )

    assert prepared is None
    assert _query_runs(db.db_path) == [("failed", "dataset_read_failed", 0)]


def test_whole_table_answer_reaches_the_pipeline(big_environment):
    """The fixed value is what the pipeline renders as the structured answer."""
    db, root, owner = big_environment
    pipeline = object.__new__(RAGPipeline)

    prepared = pipeline._attempt_structured_query(
        "排序合计是多少？", SpreadsheetQueryCoordinator(db, root), owner, KB_ID
    )

    assert prepared is not None
    assert prepared["route"] == "structured"
    assert prepared["structured_result"]["rows_scanned"] == ROW_COUNT
    assert str(sum(row[0] for row in _rows())) in prepared["answer"]
