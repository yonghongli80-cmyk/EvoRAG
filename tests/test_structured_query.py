import pytest
from pydantic import ValidationError

from rag.spreadsheets.query import (
    Metric, QueryScope, SortClause, StructuredQueryPlan,
    execute_multi_dataset_query, execute_structured_query,
)


def test_plan_schema_rejects_raw_sql():
    with pytest.raises(ValidationError):
        StructuredQueryPlan.model_validate({"dataset_id": 1, "operation": "aggregate", "sql": "SELECT *"})


def test_decimal_average_is_exact(tmp_path):
    db_path = tmp_path / "dataset.sqlite3"
    import sqlite3

    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE sheet_manifest (sheet_key TEXT PRIMARY KEY, table_name TEXT NOT NULL);
        CREATE TABLE column_manifest (sheet_key TEXT, column_key TEXT, display_name TEXT, inferred_type TEXT);
        CREATE TABLE sheet_0001 (__row_number INTEGER, c_0001 TEXT);
        INSERT INTO sheet_manifest VALUES ('sheet_0001', 'sheet_0001');
        INSERT INTO column_manifest VALUES ('sheet_0001', 'c_0001', 'amount', 'decimal');
        INSERT INTO sheet_0001 VALUES (2, '0.1'), (3, '0.2');
        """
    )
    connection.close()
    plan = StructuredQueryPlan(dataset_id=1, operation="aggregate", metrics=(Metric(column_key="c_0001", function="average"),))

    result = execute_structured_query(plan, QueryScope(dataset_paths={1: db_path}, allowed_dataset_ids=frozenset({1})))

    assert result.values[0].canonical == "0.15"
    assert result.citations[0].cell_range == "sheet_0001!c_0001:2-3"


def _sales_dataset(path, rows):
    import sqlite3
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sheet_manifest (sheet_key TEXT PRIMARY KEY, table_name TEXT NOT NULL);
        CREATE TABLE column_manifest (sheet_key TEXT, column_key TEXT, display_name TEXT, inferred_type TEXT);
        CREATE TABLE sheet_0001 (__row_number INTEGER, c_0001 TEXT, c_0002 TEXT);
        INSERT INTO sheet_manifest VALUES ('sheet_0001', 'sheet_0001');
        INSERT INTO column_manifest VALUES ('sheet_0001', 'c_0001', '品类', 'text');
        INSERT INTO column_manifest VALUES ('sheet_0001', 'c_0002', '销售额', 'decimal');
        """
    )
    connection.executemany("INSERT INTO sheet_0001 VALUES (?, ?, ?)", rows)
    connection.commit()
    connection.close()


def test_top_n_orders_typed_values(tmp_path):
    path = tmp_path / "one.sqlite3"
    _sales_dataset(path, [(2, "A", "30"), (3, "B", "10"), (4, "C", "20")])
    plan = StructuredQueryPlan(
        dataset_ids=(1,), operation="lookup", select_columns=("c_0001", "c_0002"),
        order_by=(SortClause(column_key="c_0002", direction="desc"),), limit=3,
    )
    result = execute_multi_dataset_query(plan, QueryScope({1: path}, frozenset({1})))
    assert [row.values for row in result.rows] == [
        {"c_0001": "A", "c_0002": "30"},
        {"c_0001": "C", "c_0002": "20"},
        {"c_0001": "B", "c_0002": "10"},
    ]


def test_sum_combines_compatible_datasets(tmp_path):
    first, second = tmp_path / "one.sqlite3", tmp_path / "two.sqlite3"
    _sales_dataset(first, [(2, "A", "30"), (3, "B", "20")])
    _sales_dataset(second, [(2, "C", "40"), (3, "D", "10")])
    plan = StructuredQueryPlan(
        dataset_ids=(1, 2), operation="aggregate",
        metrics=(Metric(column_key="c_0002", function="sum"),),
    )
    result = execute_multi_dataset_query(plan, QueryScope({1: first, 2: second}, frozenset({1, 2})))
    assert result.values[0].canonical == "100"
    assert {citation.dataset_id for citation in result.citations} == {1, 2}


def test_grouped_sum_combines_compatible_datasets(tmp_path):
    first, second = tmp_path / "one.sqlite3", tmp_path / "two.sqlite3"
    _sales_dataset(first, [(2, "相机", "30"), (3, "镜头", "20")])
    _sales_dataset(second, [(2, "相机", "40"), (3, "镜头", "10")])
    plan = StructuredQueryPlan(dataset_ids=(1, 2), operation="aggregate", group_by=("c_0001",), metrics=(Metric(column_key="c_0002", function="sum"),))
    result = execute_multi_dataset_query(plan, QueryScope({1: first, 2: second}, frozenset({1, 2})))
    assert [row.values for row in result.rows] == [
        {"c_0001": "相机", "c_0002_sum": "70"},
        {"c_0001": "镜头", "c_0002_sum": "30"},
    ]
