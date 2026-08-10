"""Write parsed spreadsheets to an isolated, immutable version SQLite database."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from rag.spreadsheets.models import ColumnType
from rag.spreadsheets.parser import ParsedSheet, ParsedWorkbook


@dataclass(frozen=True)
class DatasetSheetManifest:
    sheet_key: str
    table_name: str
    row_count: int
    column_count: int


@dataclass(frozen=True)
class DatasetManifest:
    sheets: tuple[DatasetSheetManifest, ...]


def resolve_dataset_path(root: Path, document_id: int, version_id: int) -> Path:
    """Return the only allowed storage path for an immutable document version."""
    if not isinstance(document_id, int) or document_id <= 0:
        raise ValueError("document_id must be a positive integer")
    if not isinstance(version_id, int) or version_id <= 0:
        raise ValueError("version_id must be a positive integer")
    return Path(root) / str(document_id) / str(version_id) / "dataset.sqlite3"


def canonical_cell(value: object, inferred: ColumnType) -> object | None:
    """Convert values to stable SQLite representations without collapsing falsey values."""
    if value is None:
        return None
    if inferred == ColumnType.BOOLEAN:
        return 1 if value is True else 0
    if inferred == ColumnType.DECIMAL:
        return format(Decimal(str(value)), "f")
    if inferred in {ColumnType.DATE, ColumnType.DATETIME}:
        if not isinstance(value, (date, datetime)):
            raise ValueError("date column contains a non-date value")
        if inferred == ColumnType.DATE and isinstance(value, datetime):
            return value.date().isoformat()
        return value.isoformat()
    return value


def write_dataset(db_path: Path, workbook: ParsedWorkbook) -> DatasetManifest:
    """Build a version database, verify it, then atomically publish it at *db_path*."""
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f"{target.name}.{uuid.uuid4().hex}.staging")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(staging)
        _create_manifest_schema(connection)
        manifests = tuple(_write_sheet(connection, sheet) for sheet in workbook.sheets)
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"dataset SQLite integrity check failed: {integrity}")
        connection.close()
        connection = None
        os.replace(staging, target)
        return DatasetManifest(sheets=manifests)
    except Exception:
        if connection is not None:
            connection.close()
        staging.unlink(missing_ok=True)
        raise


def _create_manifest_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE dataset_manifest (
            format TEXT NOT NULL,
            sheet_count INTEGER NOT NULL
        );
        CREATE TABLE sheet_manifest (
            sheet_key TEXT PRIMARY KEY,
            table_name TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            visibility TEXT NOT NULL,
            header_row INTEGER,
            first_data_row INTEGER,
            row_count INTEGER NOT NULL,
            column_count INTEGER NOT NULL
        );
        CREATE TABLE column_manifest (
            sheet_key TEXT NOT NULL,
            column_key TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            inferred_type TEXT NOT NULL,
            number_format TEXT NOT NULL,
            PRIMARY KEY(sheet_key, column_key)
        );
        CREATE TABLE formula_cells (
            sheet_key TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            column_key TEXT NOT NULL,
            formula TEXT NOT NULL,
            cached_value,
            PRIMARY KEY(sheet_key, row_number, column_key)
        );
        CREATE TABLE merged_ranges (
            sheet_key TEXT NOT NULL,
            range_ref TEXT NOT NULL,
            PRIMARY KEY(sheet_key, range_ref)
        );
        """
    )


def _write_sheet(connection: sqlite3.Connection, sheet: ParsedSheet) -> DatasetSheetManifest:
    table_name = sheet.sheet_key
    columns_sql = ", ".join(f'"{column.column_key}"' for column in sheet.columns)
    connection.execute(
        f'CREATE TABLE "{table_name}" (__row_number INTEGER NOT NULL, __row_hash TEXT NOT NULL'
        + (f", {columns_sql}" if columns_sql else "")
        + ")"
    )
    connection.execute(
        "INSERT INTO sheet_manifest "
        "(sheet_key, table_name, name, ordinal, visibility, header_row, first_data_row, row_count, column_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sheet.sheet_key,
            table_name,
            sheet.name,
            sheet.ordinal,
            sheet.visibility.value,
            sheet.header_row,
            sheet.first_data_row,
            len(sheet.rows),
            len(sheet.columns),
        ),
    )
    connection.executemany(
        "INSERT INTO column_manifest "
        "(sheet_key, column_key, ordinal, display_name, normalized_name, inferred_type, number_format) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                sheet.sheet_key,
                column.column_key,
                column.ordinal,
                column.display_name,
                column.normalized_name,
                column.inferred_type.value,
                column.number_format,
            )
            for column in sheet.columns
        ],
    )
    connection.executemany(
        "INSERT INTO merged_ranges (sheet_key, range_ref) VALUES (?, ?)",
        [(sheet.sheet_key, value) for value in sheet.merged_ranges],
    )
    connection.executemany(
        "INSERT INTO formula_cells (sheet_key, row_number, column_key, formula, cached_value) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                sheet.sheet_key,
                formula.row_number,
                formula.column_key,
                formula.formula,
                canonical_cell(
                    formula.cached_value,
                    sheet.columns[int(formula.column_key.removeprefix("c_")) - 1].inferred_type,
                ),
            )
            for formula in sheet.formula_cells
        ],
    )
    if sheet.rows:
        insert_columns = "__row_number, __row_hash" + (
            ", " + ", ".join(f'"{column.column_key}"' for column in sheet.columns)
            if sheet.columns
            else ""
        )
        marks = ", ".join("?" for _ in range(2 + len(sheet.columns)))
        connection.executemany(
            f'INSERT INTO "{table_name}" ({insert_columns}) VALUES ({marks})',
            [_canonical_row(row, sheet) for row in sheet.rows],
        )
    return DatasetSheetManifest(
        sheet_key=sheet.sheet_key,
        table_name=table_name,
        row_count=len(sheet.rows),
        column_count=len(sheet.columns),
    )


def _canonical_row(row, sheet: ParsedSheet) -> tuple[object, ...]:
    values = tuple(
        canonical_cell(cell.value, column.inferred_type)
        for cell, column in zip(row.cells, sheet.columns, strict=True)
    )
    normalized = json.dumps(values, ensure_ascii=False, default=str, separators=(",", ":"))
    row_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return (row.row_number, row_hash, *values)
