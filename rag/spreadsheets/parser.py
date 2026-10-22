"""Parse XLSX and CSV into a safe, typed in-memory workbook representation."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from config import settings
from rag.spreadsheets.models import ColumnType, SheetVisibility


class SpreadsheetParseError(ValueError):
    """Raised when a supported spreadsheet cannot be safely parsed."""


class SpreadsheetLimitError(SpreadsheetParseError):
    """Raised when an input exceeds a configured, explainable limit."""


@dataclass(frozen=True)
class ImportMapping:
    """User-confirmed import decisions for ambiguous headers (reserved for the preview API)."""

    header_row: int
    first_data_row: int


@dataclass(frozen=True)
class ParsedCell:
    value: object | None
    number_format: str = ""


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    cells: tuple[ParsedCell, ...]


@dataclass(frozen=True)
class FormulaCell:
    row_number: int
    column_key: str
    formula: str
    cached_value: object | None


@dataclass(frozen=True)
class ParsedColumn:
    column_key: str
    ordinal: int
    display_name: str
    normalized_name: str
    inferred_type: ColumnType
    number_format: str


@dataclass(frozen=True)
class ParsedSheet:
    sheet_key: str
    name: str
    ordinal: int
    visibility: SheetVisibility
    header_row: int | None
    first_data_row: int | None
    headers: tuple[str, ...]
    columns: tuple[ParsedColumn, ...]
    rows: tuple[ParsedRow, ...]
    formula_cells: tuple[FormulaCell, ...]
    merged_ranges: tuple[str, ...]


@dataclass(frozen=True)
class ParsedWorkbook:
    format: str
    sheets: tuple[ParsedSheet, ...]
    needs_mapping: bool = False
    issues: tuple[str, ...] = ()


_INTEGER = re.compile(r"^[+-]?\d+$")
_DECIMAL = re.compile(r"^[+-]?(?:\d+\.\d*|\d*\.\d+)$")

#: Leading characters Excel/LibreOffice may evaluate as a formula in a text cell.
#: A CSV import is untrusted input, so text cells that start with one of these
#: are neutralized with an apostrophe prefix instead of being stored verbatim.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_FORMULA_ESCAPE = "'"


def parse_spreadsheet(path: Path, fmt: str, mapping: ImportMapping | None) -> ParsedWorkbook:
    """Parse exactly XLSX or CSV, preserving values without executing formulas."""
    path = Path(path)
    if fmt not in {"xlsx", "csv"}:
        raise SpreadsheetParseError("unsupported spreadsheet format")
    if not path.is_file():
        raise SpreadsheetParseError("spreadsheet source does not exist")
    if path.stat().st_size > settings.spreadsheet_max_bytes:
        raise SpreadsheetLimitError("spreadsheet exceeds byte limit")
    if fmt == "xlsx":
        return _parse_xlsx(path, mapping)
    return _parse_csv(path, mapping)


def _parse_xlsx(path: Path, mapping: ImportMapping | None) -> ParsedWorkbook:
    try:
        book = load_workbook(path, read_only=False, data_only=False, keep_links=False)
    except Exception as exc:
        raise SpreadsheetParseError("could not read xlsx workbook") from exc
    try:
        if len(book.worksheets) > settings.spreadsheet_max_sheets:
            raise SpreadsheetLimitError("spreadsheet exceeds sheet limit")
        cache_book = load_workbook(path, read_only=False, data_only=True, keep_links=False)
        try:
            sheets = []
            issues: set[str] = set()
            total_rows = 0
            for ordinal, sheet in enumerate(book.worksheets, start=1):
                parsed, sheet_issues, data_rows = _parse_xlsx_sheet(
                    sheet, cache_book[sheet.title], ordinal, mapping
                )
                total_rows += data_rows
                if total_rows > settings.spreadsheet_max_rows:
                    raise SpreadsheetLimitError("spreadsheet exceeds row limit")
                sheets.append(parsed)
                issues.update(sheet_issues)
        finally:
            cache_book.close()
    finally:
        book.close()
    return ParsedWorkbook(
        format="xlsx",
        sheets=tuple(sheets),
        needs_mapping=bool(issues),
        issues=tuple(sorted(issues)),
    )


def _parse_xlsx_sheet(sheet, cached_sheet, ordinal: int, mapping: ImportMapping | None):
    if sheet.max_column > settings.spreadsheet_max_columns:
        raise SpreadsheetLimitError("spreadsheet exceeds column limit")
    header_row = mapping.header_row if mapping else _first_nonempty_row(sheet)
    first_data_row = mapping.first_data_row if mapping else (header_row + 1 if header_row else None)
    width = sheet.max_column
    headers = _headers_from_row(sheet, header_row, width)
    issues = _header_issues(sheet, header_row, headers)
    rows: list[ParsedRow] = []
    formula_cells: list[FormulaCell] = []
    if first_data_row is not None:
        for row_number in range(first_data_row, sheet.max_row + 1):
            cells = tuple(
                _xlsx_cell(sheet.cell(row_number, column), row_number, column, cached_sheet, formula_cells)
                for column in range(1, width + 1)
            )
            if any(cell.value is not None for cell in cells):
                rows.append(ParsedRow(row_number=row_number, cells=cells))
    columns = _build_columns(headers, rows)
    visibility = SheetVisibility(sheet.sheet_state)
    return (
        ParsedSheet(
            sheet_key=f"sheet_{ordinal:04d}",
            name=sheet.title,
            ordinal=ordinal,
            visibility=visibility,
            header_row=header_row,
            first_data_row=first_data_row,
            headers=headers,
            columns=columns,
            rows=tuple(rows),
            formula_cells=tuple(formula_cells),
            merged_ranges=tuple(str(value) for value in sheet.merged_cells.ranges),
        ),
        issues,
        len(rows),
    )


def _xlsx_cell(sheet_cell, row_number, column, cached_sheet, formula_cells):
    value = sheet_cell.value
    if isinstance(value, str) and value.startswith("="):
        cached_value = cached_sheet.cell(row_number, column).value
        formula_cells.append(
            FormulaCell(
                row_number=row_number,
                column_key=f"c_{column:04d}",
                formula=value,
                cached_value=cached_value,
            )
        )
        value = cached_value
    _validate_cell(value)
    return ParsedCell(value=value, number_format=sheet_cell.number_format or "")


def _parse_csv(path: Path, mapping: ImportMapping | None) -> ParsedWorkbook:
    text = _decode_csv(path)
    try:
        # Sniff only a prefix of the decoded text; the dialect object is reused below.
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    # Feed the reader the decoded text directly.  ``str.splitlines()`` would cut a
    # quoted field that contains a newline into two physical lines, shifting every
    # following record by one row; ``csv.reader`` reassembles quoted newlines when
    # it consumes an iterable of lines that keeps the record terminator.
    records = list(csv.reader(io.StringIO(text, newline=""), dialect))
    if not records:
        records = [[]]
    width = max((len(row) for row in records), default=0)
    if width > settings.spreadsheet_max_columns:
        raise SpreadsheetLimitError("spreadsheet exceeds column limit")
    header_row = mapping.header_row if mapping else 1
    first_data_row = mapping.first_data_row if mapping else 2
    headers = tuple(_display_header(value, index) for index, value in enumerate(records[header_row - 1], start=1))
    issues = _duplicate_header_issues(headers)
    rows = []
    for row_number, record in enumerate(records[first_data_row - 1 :], start=first_data_row):
        values = tuple(_csv_cell(record[index] if index < len(record) else "") for index in range(width))
        if any(cell.value is not None for cell in values):
            rows.append(ParsedRow(row_number=row_number, cells=values))
    if len(rows) > settings.spreadsheet_max_rows:
        raise SpreadsheetLimitError("spreadsheet exceeds row limit")
    columns = _build_columns(headers, rows)
    sheet = ParsedSheet(
        sheet_key="sheet_0001",
        name="CSV",
        ordinal=1,
        visibility=SheetVisibility.VISIBLE,
        header_row=header_row,
        first_data_row=first_data_row,
        headers=headers,
        columns=columns,
        rows=tuple(rows),
        formula_cells=(),
        merged_ranges=(),
    )
    return ParsedWorkbook("csv", (sheet,), bool(issues), tuple(sorted(issues)))


def _decode_csv(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SpreadsheetParseError("could not determine CSV encoding")


def _csv_cell(value: str) -> ParsedCell:
    if value == "":
        return ParsedCell(None)
    normalized = value.strip()
    if normalized.lower() == "true":
        return ParsedCell(True)
    if normalized.lower() == "false":
        return ParsedCell(False)
    if _INTEGER.match(normalized):
        return ParsedCell(int(normalized))
    if _DECIMAL.match(normalized):
        try:
            return ParsedCell(Decimal(normalized))
        except InvalidOperation:
            pass
    return ParsedCell(_neutralize_formula_prefix(value))


def _neutralize_formula_prefix(value: str) -> str:
    """Store an untrusted CSV text cell so no client evaluates it as a formula.

    ``=`` opens a formula in Excel/Calc and ``+``/``-``/``@`` are the other
    characters that spreadsheet clients may evaluate when a cell is coerced to
    a formula.  The value is kept readable but prefixed with an apostrophe, the
    spreadsheet-native "this is text" marker.  Numeric cells never reach here:
    they were already converted by :func:`_csv_cell`.
    """
    if value.startswith(_FORMULA_PREFIXES):
        return f"{_FORMULA_ESCAPE}{value}"
    return value


def _first_nonempty_row(sheet) -> int | None:
    for row_number in range(1, sheet.max_row + 1):
        if any(sheet.cell(row_number, column).value is not None for column in range(1, sheet.max_column + 1)):
            return row_number
    return None


def _headers_from_row(sheet, header_row: int | None, width: int) -> tuple[str, ...]:
    if header_row is None:
        return tuple(_display_header(None, index) for index in range(1, width + 1))
    return tuple(
        _display_header(sheet.cell(header_row, column).value, column) for column in range(1, width + 1)
    )


def _display_header(value: Any, ordinal: int) -> str:
    return str(value).strip() if value not in (None, "") else f"Column {ordinal}"


def _header_issues(sheet, header_row: int | None, headers: tuple[str, ...]) -> set[str]:
    issues = _duplicate_header_issues(headers)
    if header_row is not None:
        for merged in sheet.merged_cells.ranges:
            if merged.min_row <= header_row <= merged.max_row:
                issues.add("merged_header")
                break
    return issues


def _duplicate_header_issues(headers: tuple[str, ...]) -> set[str]:
    meaningful = [header.casefold() for header in headers if not header.startswith("Column ")]
    return {"duplicate_header"} if len(meaningful) != len(set(meaningful)) else set()


def _build_columns(headers: tuple[str, ...], rows: list[ParsedRow]) -> tuple[ParsedColumn, ...]:
    columns = []
    for index, header in enumerate(headers, start=1):
        values = [row.cells[index - 1].value for row in rows]
        formats = [row.cells[index - 1].number_format for row in rows if row.cells[index - 1].number_format]
        columns.append(
            ParsedColumn(
                column_key=f"c_{index:04d}",
                ordinal=index,
                display_name=header,
                normalized_name=_normalize_name(header),
                inferred_type=_infer_type(values, formats),
                number_format=formats[0] if formats else "",
            )
        )
    return tuple(columns)


def _normalize_name(value: str) -> str:
    return re.sub(r"\W+", "_", value.strip().casefold()).strip("_") or "column"


def _infer_type(values: list[object | None], number_formats: list[str]) -> ColumnType:
    meaningful = [value for value in values if value is not None]
    if not meaningful:
        return ColumnType.TEXT
    if all(isinstance(value, bool) for value in meaningful):
        return ColumnType.BOOLEAN
    if all(isinstance(value, int) and not isinstance(value, bool) for value in meaningful):
        return ColumnType.INTEGER
    if all(isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) for value in meaningful):
        return ColumnType.DECIMAL
    if all(isinstance(value, datetime) for value in meaningful):
        if number_formats and all(not _format_includes_time(value) for value in number_formats):
            return ColumnType.DATE
        return ColumnType.DATETIME
    if all(isinstance(value, date) and not isinstance(value, datetime) for value in meaningful):
        return ColumnType.DATE
    if all(_is_decimal_text(value) for value in meaningful):
        return ColumnType.DECIMAL
    return ColumnType.TEXT


def _format_includes_time(number_format: str) -> bool:
    """Recognize the ordinary Excel time tokens without treating month ``m`` as minutes."""
    value = number_format.casefold()
    return any(token in value for token in ("h", "s", "am/pm"))


def _is_decimal_text(value: object) -> bool:
    if not isinstance(value, str) or not _DECIMAL.match(value.strip()):
        return False
    try:
        Decimal(value.strip())
    except InvalidOperation:
        return False
    return True


def _validate_cell(value: object | None) -> None:
    if isinstance(value, str) and len(value) > settings.spreadsheet_max_cell_chars:
        raise SpreadsheetLimitError("spreadsheet exceeds cell character limit")
