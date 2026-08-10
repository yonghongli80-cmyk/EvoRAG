"""Typed, read-only gateway for versioned spreadsheet SQLite datasets."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_IDENTIFIER = re.compile(r"^(?:sheet_\d{4}|c_\d{4})$")


class FilterClause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    column_key: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "between", "in"]
    value: str | int | float | bool | None = None
    values: tuple[str | int | float | bool, ...] = ()


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    column_key: str
    function: Literal["count", "distinct_count", "sum", "average", "min", "max"]


class StructuredQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset_id: int
    operation: Literal["lookup", "aggregate", "compare"]
    metrics: tuple[Metric, ...] = ()
    filters: tuple[FilterClause, ...] = ()
    limit: int = Field(default=100, ge=1, le=500)


@dataclass(frozen=True)
class QueryScope:
    dataset_paths: Mapping[int, Path]
    allowed_dataset_ids: frozenset[int]


@dataclass(frozen=True)
class QueryValue:
    metric: Metric
    canonical: str


@dataclass(frozen=True)
class StructuredCitation:
    dataset_id: int
    sheet_key: str
    cell_range: str


@dataclass(frozen=True)
class StructuredQueryResult:
    values: tuple[QueryValue, ...]
    citations: tuple[StructuredCitation, ...]
    rows_scanned: int


def execute_structured_query(plan: StructuredQueryPlan, scope: QueryScope) -> StructuredQueryResult:
    """Evaluate an allowlisted plan against an authorized dataset in read-only mode."""
    if plan.dataset_id not in scope.allowed_dataset_ids:
        raise PermissionError("dataset is outside query scope")
    path = scope.dataset_paths.get(plan.dataset_id)
    if path is None:
        raise ValueError("dataset path is unavailable")
    connection = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        columns = _columns(connection)
        if not plan.metrics:
            raise ValueError("query requires at least one metric")
        values: list[QueryValue] = []
        citations: list[StructuredCitation] = []
        scanned = 0
        for metric in plan.metrics:
            sheet_key, inferred = _resolve_column(columns, metric.column_key)
            rows = _select_rows(connection, sheet_key, metric.column_key, plan.filters, columns, plan.limit)
            scanned += len(rows)
            canonical = _aggregate([row[1] for row in rows], metric.function, inferred)
            values.append(QueryValue(metric, canonical))
            if rows:
                citations.append(
                    StructuredCitation(
                        plan.dataset_id,
                        sheet_key,
                        f"{sheet_key}!{metric.column_key}:{rows[0][0]}-{rows[-1][0]}",
                    )
                )
        return StructuredQueryResult(tuple(values), tuple(citations), scanned)
    finally:
        connection.close()


def _columns(connection):
    rows = connection.execute(
        "SELECT sheet_key, column_key, inferred_type FROM column_manifest"
    ).fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


def _resolve_column(columns, column_key: str) -> tuple[str, str]:
    matches = [(sheet, inferred) for (sheet, key), inferred in columns.items() if key == column_key]
    if len(matches) != 1 or not _IDENTIFIER.match(column_key):
        raise ValueError("unknown or ambiguous column key")
    return matches[0]


def _select_rows(connection, sheet_key, metric_key, filters, columns, limit):
    if not _IDENTIFIER.match(sheet_key):
        raise ValueError("invalid sheet key")
    clauses, values = [], []
    for item in filters:
        filter_sheet, _ = _resolve_column(columns, item.column_key)
        if filter_sheet != sheet_key:
            raise ValueError("cross-sheet filters are not supported")
        quoted = f'"{item.column_key}"'
        if item.operator == "contains":
            clauses.append(f"{quoted} LIKE ?")
            values.append(f"%{item.value}%")
        elif item.operator == "between":
            if len(item.values) != 2:
                raise ValueError("between requires exactly two values")
            clauses.append(f"{quoted} BETWEEN ? AND ?")
            values.extend(item.values)
        elif item.operator == "in":
            if not item.values:
                raise ValueError("in requires values")
            clauses.append(f"{quoted} IN ({', '.join('?' for _ in item.values)})")
            values.extend(item.values)
        else:
            op = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[item.operator]
            clauses.append(f"{quoted} {op} ?")
            values.append(item.value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f'SELECT __row_number, "{metric_key}" FROM "{sheet_key}"{where} ORDER BY __row_number LIMIT ?'
    return connection.execute(sql, (*values, limit)).fetchall()


def _aggregate(values, function: str, inferred: str) -> str:
    meaningful = [value for value in values if value is not None]
    if function == "count":
        return str(len(meaningful))
    if function == "distinct_count":
        return str(len(set(meaningful)))
    if function in {"sum", "average"}:
        if inferred not in {"integer", "decimal"}:
            raise ValueError("numeric aggregation requires numeric column")
        items = [Decimal(str(value)) for value in meaningful]
        total = sum(items, Decimal("0"))
        result = total if function == "sum" else (total / len(items) if items else Decimal("0"))
        return format(result, "f")
    if not meaningful:
        return ""
    return str(min(meaningful) if function == "min" else max(meaningful))
