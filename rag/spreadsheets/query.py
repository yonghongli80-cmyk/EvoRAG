"""Typed, read-only gateway for versioned spreadsheet SQLite datasets."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class SortClause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    column_key: str
    direction: Literal["asc", "desc"] = "asc"


class StructuredQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset_id: int | None = None
    dataset_ids: tuple[int, ...] = ()
    operation: Literal["lookup", "aggregate", "compare"]
    select_columns: tuple[str, ...] = ()
    metrics: tuple[Metric, ...] = ()
    filters: tuple[FilterClause, ...] = ()
    order_by: tuple[SortClause, ...] = ()
    limit: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def require_datasets(self):
        if self.dataset_id is None and not self.dataset_ids:
            raise ValueError("query requires a dataset")
        return self

    def selected_dataset_ids(self) -> tuple[int, ...]:
        values = self.dataset_ids or ((self.dataset_id,) if self.dataset_id is not None else ())
        return tuple(dict.fromkeys(values))


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
class QueryRow:
    dataset_id: int
    sheet_key: str
    row_number: int
    values: dict[str, str | int | float | bool | None]


@dataclass(frozen=True)
class StructuredQueryResult:
    values: tuple[QueryValue, ...]
    citations: tuple[StructuredCitation, ...]
    rows_scanned: int
    rows: tuple[QueryRow, ...] = ()


def execute_structured_query(plan: StructuredQueryPlan, scope: QueryScope) -> StructuredQueryResult:
    """Evaluate an allowlisted plan against an authorized dataset in read-only mode."""
    if plan.dataset_id is None:
        raise ValueError("single dataset query requires dataset_id")
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


def execute_multi_dataset_query(plan: StructuredQueryPlan, scope: QueryScope) -> StructuredQueryResult:
    """Execute one allowlisted plan across authorized compatible datasets."""
    dataset_ids = plan.selected_dataset_ids()
    if any(item not in scope.allowed_dataset_ids for item in dataset_ids):
        raise PermissionError("dataset is outside query scope")
    if plan.operation == "lookup":
        return _execute_multi_lookup(plan, scope, dataset_ids)
    if not plan.metrics:
        raise ValueError("query requires at least one metric")
    values_by_metric: list[list[Any]] = [[] for _ in plan.metrics]
    citations: list[StructuredCitation] = []
    scanned = 0
    inferred_types: list[str | None] = [None for _ in plan.metrics]
    for dataset_id in dataset_ids:
        connection = _open_dataset(scope, dataset_id)
        try:
            columns = _columns(connection)
            for index, metric in enumerate(plan.metrics):
                sheet_key, inferred = _resolve_column(columns, metric.column_key)
                if inferred_types[index] not in (None, inferred):
                    raise ValueError("incompatible metric types")
                inferred_types[index] = inferred
                rows = _select_rows(connection, sheet_key, metric.column_key, plan.filters, columns, plan.limit)
                scanned += len(rows)
                values_by_metric[index].extend(row[1] for row in rows)
                if rows:
                    citations.append(StructuredCitation(dataset_id, sheet_key, f"{sheet_key}!{metric.column_key}:{rows[0][0]}-{rows[-1][0]}"))
        finally:
            connection.close()
    values = tuple(
        QueryValue(metric, _aggregate(items, metric.function, inferred_types[index] or "text"))
        for index, (metric, items) in enumerate(zip(plan.metrics, values_by_metric, strict=True))
    )
    return StructuredQueryResult(values, tuple(citations), scanned)


def _open_dataset(scope: QueryScope, dataset_id: int):
    path = scope.dataset_paths.get(dataset_id)
    if path is None:
        raise ValueError("dataset path is unavailable")
    return sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)


def _execute_multi_lookup(plan, scope, dataset_ids):
    gathered: list[tuple[QueryRow, dict[str, str]]] = []
    citations: list[StructuredCitation] = []
    for dataset_id in dataset_ids:
        connection = _open_dataset(scope, dataset_id)
        try:
            columns = _columns(connection)
            keys = plan.select_columns or tuple(item.column_key for item in plan.order_by)
            if not keys:
                raise ValueError("lookup requires selected columns")
            resolved = [_resolve_column(columns, key) for key in keys]
            sheet_keys = {item[0] for item in resolved}
            if len(sheet_keys) != 1:
                raise ValueError("cross-sheet lookup is not supported")
            sheet_key = next(iter(sheet_keys))
            filter_clauses, parameters = _filter_sql(plan.filters, columns, sheet_key)
            quoted = ", ".join(f'"{key}"' for key in keys)
            sql = f'SELECT __row_number, {quoted} FROM "{sheet_key}"{filter_clauses}'
            for raw in connection.execute(sql, parameters).fetchall():
                values = {key: raw[index + 1] for index, key in enumerate(keys)}
                types = {key: resolved[index][1] for index, key in enumerate(keys)}
                gathered.append((QueryRow(dataset_id, sheet_key, raw[0], values), types))
        finally:
            connection.close()
    for sort in reversed(plan.order_by):
        gathered.sort(key=lambda item: _typed_sort_value(item[0].values.get(sort.column_key), item[1].get(sort.column_key, "text")), reverse=sort.direction == "desc")
    selected = tuple(item[0] for item in gathered[:plan.limit])
    for dataset_id in dataset_ids:
        rows = [item.row_number for item in selected if item.dataset_id == dataset_id]
        if rows:
            citations.append(StructuredCitation(dataset_id, selected[0].sheet_key, f"{selected[0].sheet_key}!rows:{min(rows)}-{max(rows)}"))
    return StructuredQueryResult((), tuple(citations), len(gathered), selected)


def _typed_sort_value(value, inferred):
    if value is None:
        return (0, "")
    if inferred in {"integer", "decimal"}:
        return (1, Decimal(str(value)))
    return (1, str(value).casefold())


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
    where, values = _filter_sql(filters, columns, sheet_key)
    sql = f'SELECT __row_number, "{metric_key}" FROM "{sheet_key}"{where} ORDER BY __row_number LIMIT ?'
    return connection.execute(sql, (*values, limit)).fetchall()


def _filter_sql(filters, columns, sheet_key):
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
    return where, values


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
