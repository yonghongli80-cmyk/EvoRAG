from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rag.spreadsheets.query import (
    FilterClause,
    Metric,
    QueryScope,
    StructuredQueryPlan,
    execute_multi_dataset_query,
    execute_structured_query,
)

VALUES = (
    ("9.50", "A"),
    ("10.00", "A"),
    ("100.00", "A"),
    ("-2.0000000000000000000000000001", "B"),
    ("-2.0000000000000000000000000002", "B"),
)


def _dataset(path: Path, rows=VALUES) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE column_manifest (sheet_key TEXT, column_key TEXT, display_name TEXT, inferred_type TEXT);"
        "INSERT INTO column_manifest VALUES ('sheet_0001', 'c_0001', '金额', 'decimal');"
        "INSERT INTO column_manifest VALUES ('sheet_0001', 'c_0002', '分组', 'text');"
        "CREATE TABLE sheet_0001 (__row_number INTEGER PRIMARY KEY, c_0001 TEXT, c_0002 TEXT);"
    )
    connection.executemany(
        "INSERT INTO sheet_0001 (c_0001, c_0002) VALUES (?, ?)", rows
    )
    connection.commit()
    connection.close()
    return path


def _plan(*metrics, dataset_id=1, dataset_ids=(), **kwargs):
    return StructuredQueryPlan(
        dataset_id=dataset_id,
        dataset_ids=dataset_ids,
        operation="aggregate",
        sheet_key="sheet_0001",
        metrics=tuple(metrics),
        **kwargs,
    )


def _scope(paths):
    return QueryScope(paths, frozenset(paths))


def test_single_dataset_decimal_extrema_use_numeric_order(tmp_path):
    path = _dataset(tmp_path / "single.sqlite3")
    result = execute_structured_query(
        _plan(Metric(column_key="c_0001", function="min"), Metric(column_key="c_0001", function="max")),
        _scope({1: path}),
    )

    assert [value.canonical for value in result.values] == [
        "-2.0000000000000000000000000002",
        "100.00",
    ]


def test_multi_dataset_decimal_extrema_merge_numerically(tmp_path):
    first = _dataset(tmp_path / "first.sqlite3", VALUES[:3])
    second = _dataset(tmp_path / "second.sqlite3", VALUES[3:])
    result = execute_multi_dataset_query(
        _plan(
            Metric(column_key="c_0001", function="min"),
            Metric(column_key="c_0001", function="max"),
            dataset_id=None,
            dataset_ids=(1, 2),
        ),
        _scope({1: first, 2: second}),
    )

    assert [value.canonical for value in result.values] == [
        "-2.0000000000000000000000000002",
        "100.00",
    ]


@pytest.mark.parametrize("function,expected_a,expected_b", [
    ("min", "9.50", "-2.0000000000000000000000000002"),
    ("max", "100.00", "-2.0000000000000000000000000001"),
])
def test_grouped_decimal_extrema_use_numeric_order(tmp_path, function, expected_a, expected_b):
    first = _dataset(tmp_path / "group-first.sqlite3", VALUES[:3])
    second = _dataset(tmp_path / "group-second.sqlite3", VALUES[3:])
    result = execute_multi_dataset_query(
        _plan(
            Metric(column_key="c_0001", function=function),
            dataset_id=None,
            dataset_ids=(1, 2),
            group_by=("c_0002",),
        ),
        _scope({1: first, 2: second}),
    )

    assert {row.values["c_0002"]: row.values[f"c_0001_{function}"] for row in result.rows} == {
        "A": expected_a,
        "B": expected_b,
    }


@pytest.mark.parametrize(
    "operator,value,values,expected",
    [
        ("gt", "9.50", (), "110.00"),
        ("between", None, ("9.50", "100.00"), "119.50"),
    ],
)
def test_decimal_filters_compare_text_cells_numerically(tmp_path, operator, value, values, expected):
    path = _dataset(tmp_path / "filtered.sqlite3")
    result = execute_multi_dataset_query(
        _plan(
            Metric(column_key="c_0001", function="sum"),
            filters=(FilterClause(column_key="c_0001", operator=operator, value=value, values=values),),
        ),
        _scope({1: path}),
    )

    assert result.values[0].canonical == expected


def test_text_filter_keeps_lexical_comparison(tmp_path):
    path = _dataset(tmp_path / "text-filter.sqlite3")
    result = execute_multi_dataset_query(
        _plan(
            Metric(column_key="c_0001", function="count"),
            filters=(FilterClause(column_key="c_0002", operator="gt", value="A"),),
        ),
        _scope({1: path}),
    )

    assert result.values[0].canonical == "2"
