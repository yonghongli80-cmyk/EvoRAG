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

_SHEET_KEY = re.compile(r"^sheet_\d{4}$")
_COLUMN_KEY = re.compile(r"^c_\d{4}$")
_NUMERIC_TYPES = frozenset({"integer", "decimal"})


class StructuredQueryInputError(ValueError):
    """A plan that cannot be executed because its own input is unusable.

    Only input-shaped problems raise this subclass: an unknown or ambiguous
    field, a cross-sheet reference, a malformed filter, or an aggregation the
    column type cannot support.  The coordinator turns it into a clarification.
    Anything else that escapes execution is an internal failure and must be
    reported as one instead of being dressed up as a question for the user.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FilterClause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    column_key: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "between", "in"]
    value: str | int | float | bool | None = None
    values: tuple[str | int | float | bool, ...] = ()
    sheet_key: str | None = None


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    column_key: str
    function: Literal["count", "distinct_count", "sum", "average", "min", "max"]
    sheet_key: str | None = None


class SortClause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    column_key: str
    direction: Literal["asc", "desc"] = "asc"
    sheet_key: str | None = None


class StructuredQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset_id: int | None = None
    dataset_ids: tuple[int, ...] = ()
    operation: Literal["lookup", "aggregate", "compare"]
    # Sheet scope of the plan: column keys are only unique inside one sheet, so
    # a plan that is meant to read one table carries that table's key.  ``None``
    # keeps the legacy dataset-wide lookup and still requires a unique key.
    sheet_key: str | None = None
    select_columns: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()
    metrics: tuple[Metric, ...] = ()
    filters: tuple[FilterClause, ...] = ()
    order_by: tuple[SortClause, ...] = ()
    # Row-output budget of list/lookup plans.  Aggregates are evaluated by
    # SQLite over the whole filtered table, so this never bounds the rows a
    # metric is computed from.
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
    connection = _connect(path)
    try:
        columns = _columns(connection)
        if not plan.metrics:
            raise ValueError("query requires at least one metric")
        values: list[QueryValue] = []
        citations: list[StructuredCitation] = []
        scanned = 0
        for metric in plan.metrics:
            sheet_key, inferred = _resolve_column(
                columns, metric.column_key, _scope(metric.sheet_key, plan.sheet_key)
            )
            partial = _partial_aggregate(
                connection, sheet_key, metric, plan.filters, columns, plan.sheet_key, inferred
            )
            scanned += partial.rows
            values.append(QueryValue(metric, _merge_partials((partial,), metric.function, inferred)))
            if partial.rows:
                citations.append(
                    StructuredCitation(
                        plan.dataset_id,
                        sheet_key,
                        f"{sheet_key}!{metric.column_key}:{partial.first_row}-{partial.last_row}",
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
    if plan.group_by:
        return _execute_grouped_aggregate(plan, scope, dataset_ids)
    if not plan.metrics:
        raise ValueError("query requires at least one metric")
    partials_by_metric: list[list[_PartialAggregate]] = [[] for _ in plan.metrics]
    citations: list[StructuredCitation] = []
    scanned = 0
    inferred_types: list[str | None] = [None for _ in plan.metrics]
    for dataset_id in dataset_ids:
        connection = _open_dataset(scope, dataset_id)
        try:
            columns = _columns(connection)
            for index, metric in enumerate(plan.metrics):
                sheet_key, inferred = _resolve_column(
                    columns, metric.column_key, _scope(metric.sheet_key, plan.sheet_key)
                )
                if inferred_types[index] not in (None, inferred):
                    raise ValueError("incompatible metric types")
                inferred_types[index] = inferred
                partial = _partial_aggregate(
                    connection, sheet_key, metric, plan.filters, columns, plan.sheet_key, inferred
                )
                partials_by_metric[index].append(partial)
                scanned += partial.rows
                if partial.rows:
                    citations.append(
                        StructuredCitation(
                            dataset_id,
                            sheet_key,
                            f"{sheet_key}!{metric.column_key}:{partial.first_row}-{partial.last_row}",
                        )
                    )
        finally:
            connection.close()
    values = tuple(
        QueryValue(metric, _merge_partials(items, metric.function, inferred_types[index] or "text"))
        for index, (metric, items) in enumerate(zip(plan.metrics, partials_by_metric, strict=True))
    )
    return StructuredQueryResult(values, tuple(citations), scanned)


def _execute_grouped_aggregate(plan, scope, dataset_ids):
    metric = plan.metrics[0]
    grouped: dict[tuple, list[Any]] = {}
    citations, scanned = [], 0
    inferred = None
    for dataset_id in dataset_ids:
        connection = _open_dataset(scope, dataset_id)
        try:
            columns = _columns(connection)
            sheet_key, metric_type = _resolve_column(
                columns, metric.column_key, _scope(metric.sheet_key, plan.sheet_key)
            )
            for key in plan.group_by:
                group_sheet, _ = _resolve_column(columns, key, plan.sheet_key)
                if group_sheet != sheet_key:
                    raise StructuredQueryInputError(
                        "cross_sheet_unsupported", "cross-sheet grouping is not supported"
                    )
            where, parameters = _filter_sql(plan.filters, columns, sheet_key, plan.sheet_key)
            selected = ", ".join(_quoted_column(key) for key in (*plan.group_by, metric.column_key))
            rows = connection.execute(f'SELECT __row_number, {selected} FROM {_quoted_sheet(sheet_key)}{where}', parameters).fetchall()
            scanned += len(rows)
            for row in rows:
                grouped.setdefault(tuple(row[1:1 + len(plan.group_by)]), []).append(row[-1])
            if rows:
                citations.append(StructuredCitation(dataset_id, sheet_key, f"{sheet_key}!rows:{rows[0][0]}-{rows[-1][0]}"))
            inferred = metric_type
        finally:
            connection.close()
    result_rows = []
    for index, (key, values) in enumerate(grouped.items(), start=1):
        payload = dict(zip(plan.group_by, key, strict=True))
        payload[f"{metric.column_key}_{metric.function}"] = _aggregate(values, metric.function, inferred or "text")
        result_rows.append(QueryRow(0, "combined", index, payload))
    return StructuredQueryResult((), tuple(citations), scanned, tuple(result_rows[:plan.limit]))


def _connect(path) -> sqlite3.Connection:
    """Open one dataset read-only and register the exact decimal aggregate."""
    connection = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    connection.create_aggregate("rag_decimal_sum", 1, _ExactDecimalSum)
    connection.create_collation("rag_decimal", _compare_decimal)
    return connection


def _compare_decimal(left: str, right: str) -> int:
    """Compare canonical decimal text without converting through binary floats."""
    left_value, right_value = Decimal(left), Decimal(right)
    return (left_value > right_value) - (left_value < right_value)


def _open_dataset(scope: QueryScope, dataset_id: int):
    path = scope.dataset_paths.get(dataset_id)
    if path is None:
        raise ValueError("dataset path is unavailable")
    return _connect(path)


def _execute_multi_lookup(plan, scope, dataset_ids):
    gathered: list[tuple[QueryRow, dict[str, str]]] = []
    citations: list[StructuredCitation] = []
    for dataset_id in dataset_ids:
        connection = _open_dataset(scope, dataset_id)
        try:
            columns = _columns(connection)
            if plan.select_columns:
                refs = [(key, plan.sheet_key) for key in plan.select_columns]
            else:
                refs = [
                    (item.column_key, _scope(item.sheet_key, plan.sheet_key))
                    for item in plan.order_by
                ]
            if not refs:
                raise StructuredQueryInputError("invalid_query_plan", "lookup requires selected columns")
            resolved = [_resolve_column(columns, key, sheet_key) for key, sheet_key in refs]
            sheet_keys = {item[0] for item in resolved}
            if len(sheet_keys) != 1:
                raise StructuredQueryInputError("cross_sheet_unsupported", "cross-sheet lookup is not supported")
            sheet_key = next(iter(sheet_keys))
            filter_clauses, parameters = _filter_sql(plan.filters, columns, sheet_key, plan.sheet_key)
            keys = [key for key, _ in refs]
            quoted = ", ".join(_quoted_column(key) for key in keys)
            sql = f'SELECT __row_number, {quoted} FROM {_quoted_sheet(sheet_key)}{filter_clauses}'
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


def _scope(sheet_key: str | None, default: str | None) -> str | None:
    """Return the sheet scope of one clause without letting it contradict the plan."""
    if sheet_key is None:
        return default
    if default is not None and sheet_key != default:
        raise StructuredQueryInputError("conflicting_sheet_scope", "conflicting sheet scope")
    return sheet_key


def _quoted_sheet(sheet_key: str) -> str:
    """Quote a manifest sheet name; only the generated ``sheet_NNNN`` form passes."""
    if not _SHEET_KEY.match(sheet_key):
        raise StructuredQueryInputError("invalid_field_reference", "invalid sheet key")
    return f'"{sheet_key}"'


def _quoted_column(column_key: str) -> str:
    """Quote a manifest column name; only the generated ``c_NNNN`` form passes."""
    if not _COLUMN_KEY.match(column_key):
        raise StructuredQueryInputError("invalid_field_reference", "invalid column key")
    return f'"{column_key}"'


def _resolve_column(columns, column_key: str, sheet_key: str | None = None) -> tuple[str, str]:
    """Resolve one column reference to ``(sheet_key, inferred_type)``.

    Column keys are numbered per sheet, so a key alone is not a reference.  A
    sheet-scoped reference must exist in that sheet's manifest entry.  Without a
    sheet scope the key must still be unique in the whole dataset: an ambiguous
    key is an error, never a silently chosen table.
    """
    if sheet_key is not None:
        if not _SHEET_KEY.match(sheet_key) or not _COLUMN_KEY.match(column_key):
            raise StructuredQueryInputError("invalid_field_reference", "invalid sheet or column key")
        inferred = columns.get((sheet_key, column_key))
        if inferred is None:
            raise StructuredQueryInputError("unknown_field", "unknown column key in sheet")
        return sheet_key, inferred
    matches = [(sheet, inferred) for (sheet, key), inferred in columns.items() if key == column_key]
    if len(matches) != 1 or not _COLUMN_KEY.match(column_key):
        raise StructuredQueryInputError("unknown_field", "unknown or ambiguous column key")
    return matches[0]


class _ExactDecimalSum:
    """Exact decimal accumulator registered into SQLite for ``sum``/``average``.

    SQLite has no decimal type: its builtin ``SUM()``/``AVG()`` coerce the
    canonical decimal *strings* written by ``rag.spreadsheets.store`` to binary
    floats, so ``0.1 + 0.2`` would come back as ``0.30000000000000004``.
    Registering this accumulator keeps the whole aggregation inside one filtered
    SQL statement over the entire table while keeping decimal precision.  An
    average needs no separate aggregate: it is an exact total over an exact
    count, both of which the same statement already returns.
    """

    def __init__(self) -> None:
        self._total = Decimal("0")

    def step(self, value) -> None:
        if value is None:
            return
        self._total += Decimal(str(value))

    def finalize(self) -> str:
        return format(self._total, "f")


@dataclass(frozen=True)
class _PartialAggregate:
    """One dataset's contribution to one metric, evaluated inside SQLite.

    The row set is never limited: ``plan.limit`` bounds list-style output, not
    the rows a value is computed from.  ``rows``/``first_row``/``last_row``
    describe the rows the aggregate actually consumed, so ``rows_scanned`` and
    the citation stay honest without materializing a single row in Python.
    """

    rows: int
    first_row: int | None
    last_row: int | None
    non_null: int = 0
    total: Decimal = Decimal("0")
    extreme: str = ""
    distinct: tuple[Any, ...] = ()


# Everything except ``distinct_count`` fits in one row-free SQL statement;
# distinct counts cannot be added across datasets, so they merge their values.
_AGGREGATE_EXPRESSIONS = {
    "count": "COUNT({column})",
    "sum": "rag_decimal_sum({column})",
    "average": "rag_decimal_sum({column})",
    "min": "MIN({column})",
    "max": "MAX({column})",
}


def _partial_aggregate(
    connection, sheet_key, metric, filters, columns, plan_sheet_key, inferred
) -> _PartialAggregate:
    """Evaluate one metric for one dataset entirely inside SQLite.

    Aggregates are pushed down instead of reading rows and computing in Python:
    the previous ``ORDER BY __row_number LIMIT ?`` prefix made every value a
    statement about at most the first 100 rows of a table.
    """
    function = metric.function
    if function in {"sum", "average"} and inferred not in _NUMERIC_TYPES:
        # Refuse loudly: silently counting or summing text would answer a
        # different question than the one that was asked.
        raise StructuredQueryInputError(
            "non_numeric_metric", "numeric aggregation requires numeric column"
        )
    where, parameters = _filter_sql(filters, columns, sheet_key, plan_sheet_key)
    quoted_column = _quoted_column(metric.column_key)
    quoted_sheet = _quoted_sheet(sheet_key)
    bounds = connection.execute(
        f"SELECT COUNT(*), MIN(__row_number), MAX(__row_number) FROM {quoted_sheet}{where}",
        parameters,
    ).fetchone()
    rows = int(bounds[0] or 0)
    first_row = bounds[1] if rows else None
    last_row = bounds[2] if rows else None
    if function == "distinct_count":
        present = f"{quoted_column} IS NOT NULL"
        clause = f"{where} AND {present}" if where else f" WHERE {present}"
        distinct = connection.execute(
            f"SELECT DISTINCT {quoted_column} FROM {quoted_sheet}{clause}", parameters
        ).fetchall()
        return _PartialAggregate(
            rows, first_row, last_row, distinct=tuple(row[0] for row in distinct)
        )
    aggregate = _AGGREGATE_EXPRESSIONS[function].format(column=quoted_column)
    if function in {"min", "max"} and inferred == "decimal":
        aggregate = f"{function.upper()}({quoted_column} COLLATE rag_decimal)"
    column_count, value = connection.execute(
        f"SELECT COUNT({quoted_column}), "
        f"{aggregate} "
        f"FROM {quoted_sheet}{where}",
        parameters,
    ).fetchone()
    non_null = int(column_count or 0)
    if function in {"sum", "average"}:
        return _PartialAggregate(
            rows, first_row, last_row, non_null, Decimal(str(value or "0"))
        )
    if function in {"min", "max"}:
        return _PartialAggregate(
            rows, first_row, last_row, non_null, extreme="" if value is None else str(value)
        )
    return _PartialAggregate(rows, first_row, last_row, non_null)


def _merge_partials(partials, function: str, inferred: str) -> str:
    """Combine per-dataset partials into the canonical value of one metric.

    Every partial is already an exact SQL aggregate, so merging them is exact
    too: counts add up, sums add up, an average is the summed total over the
    summed count, and extremes compare the per-dataset extremes.
    """
    if function == "count":
        return str(sum(item.non_null for item in partials))
    if function == "distinct_count":
        return str(len({value for item in partials for value in item.distinct}))
    if function == "sum":
        return format(sum((item.total for item in partials), Decimal("0")), "f")
    if function == "average":
        total = sum((item.total for item in partials), Decimal("0"))
        count = sum(item.non_null for item in partials)
        return format(total / count, "f") if count else "0"
    candidates = [item.extreme for item in partials if item.extreme != ""]
    if not candidates:
        return ""
    pick = min if function == "min" else max
    if inferred in {"integer", "decimal", "boolean"}:
        # Numeric columns compare numerically; text columns retain lexical order.
        return pick(candidates, key=Decimal)
    return pick(candidates)


def _filter_sql(filters, columns, sheet_key, plan_sheet_key=None):
    clauses, values = [], []
    for item in filters:
        # A filter may declare its own sheet; it then has to be the sheet being
        # read, which is the same cross-sheet rule as before but now explicit.
        filter_sheet, inferred = _resolve_column(
            columns,
            item.column_key,
            item.sheet_key if item.sheet_key is not None else plan_sheet_key,
        )
        if filter_sheet != sheet_key:
            raise StructuredQueryInputError("cross_sheet_unsupported", "cross-sheet filters are not supported")
        quoted = _quoted_column(item.column_key)
        compared = f"{quoted} COLLATE rag_decimal" if inferred == "decimal" else quoted
        if item.operator == "contains":
            clauses.append(f"{quoted} LIKE ?")
            values.append(f"%{item.value}%")
        elif item.operator == "between":
            if len(item.values) != 2:
                raise StructuredQueryInputError("invalid_filter", "between requires exactly two values")
            clauses.append(f"{compared} BETWEEN ? AND ?")
            values.extend(item.values)
        elif item.operator == "in":
            if not item.values:
                raise StructuredQueryInputError("invalid_filter", "in requires values")
            clauses.append(f"{compared} IN ({', '.join('?' for _ in item.values)})")
            values.extend(item.values)
        else:
            op = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[item.operator]
            clauses.append(f"{compared} {op} ?")
            values.append(item.value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, values


def _aggregate(values, function: str, inferred: str) -> str:
    """Python aggregate used by the grouped merge, which reads every row."""
    meaningful = [value for value in values if value is not None]
    if function == "count":
        return str(len(meaningful))
    if function == "distinct_count":
        return str(len(set(meaningful)))
    if function in {"sum", "average"}:
        if inferred not in _NUMERIC_TYPES:
            raise StructuredQueryInputError(
                "non_numeric_metric", "numeric aggregation requires numeric column"
            )
        items = [Decimal(str(value)) for value in meaningful]
        total = sum(items, Decimal("0"))
        result = total if function == "sum" else (total / len(items) if items else Decimal("0"))
        return format(result, "f")
    if not meaningful:
        return ""
    if inferred == "decimal":
        key = Decimal
        return str((min if function == "min" else max)(meaningful, key=key))
    return str(min(meaningful) if function == "min" else max(meaningful))
