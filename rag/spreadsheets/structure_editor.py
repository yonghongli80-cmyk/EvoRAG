"""Conservative structural edits that always produce a new `.xlsx` file."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from rag.spreadsheets.edit_models import (
    SpreadsheetStructureAction,
    SpreadsheetStructureEditRequest,
)

_INVALID_SHEET_NAME = re.compile(r"[\\/*?:\[\]]")
_MAX_ROW = 1_048_576
_MAX_COLUMN = 16_384


class WorkbookStructureError(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class StructureChange:
    action: SpreadsheetStructureAction
    sheet_key: str | None
    sheet_name: str | None
    index: int | None
    count: int
    new_sheet_name: str | None
    affected_nonempty_cells: int


def _load(path: Path):
    try:
        return load_workbook(
            path,
            data_only=False,
            read_only=False,
            keep_links=True,
        )
    except Exception as exc:
        raise WorkbookStructureError("workbook_open_failed") from exc


def _contains_formulas(book) -> bool:
    return any(
        cell.data_type == "f"
        for sheet in book.worksheets
        for row in sheet.iter_rows()
        for cell in row
    )


def _has_unsupported_dependencies(book) -> bool:
    if getattr(book, "_external_links", ()):
        return True
    if len(book.defined_names):
        return True
    return any(
        bool(sheet.tables)
        or bool(getattr(sheet, "_charts", ()))
        or bool(getattr(sheet, "_pivots", ()))
        for sheet in book.worksheets
    )


def _validate_row_or_column_edit(book, sheet) -> None:
    if bool(book.security.lockStructure) or bool(sheet.protection.sheet):
        raise WorkbookStructureError("protected_workbook")
    if _contains_formulas(book):
        raise WorkbookStructureError("workbook_contains_formulas")
    if sheet.merged_cells.ranges:
        raise WorkbookStructureError("sheet_contains_merged_cells")
    if _has_unsupported_dependencies(book):
        raise WorkbookStructureError("unsupported_workbook_dependencies")


def _validate_index(sheet, request: SpreadsheetStructureEditRequest) -> None:
    assert request.index is not None
    is_row = request.action.endswith("rows")
    maximum = _MAX_ROW if is_row else _MAX_COLUMN
    used = sheet.max_row if is_row else sheet.max_column
    final_index = request.index + request.count - 1
    if final_index > maximum:
        raise WorkbookStructureError("invalid_structure_index")
    if request.action.startswith("insert_"):
        if request.index > used + 1:
            raise WorkbookStructureError("invalid_structure_index")
    elif request.index > used or final_index > used:
        raise WorkbookStructureError("invalid_structure_index")


def _nonempty_delete_count(sheet, request: SpreadsheetStructureEditRequest) -> int:
    assert request.index is not None
    end = request.index + request.count - 1
    if request.action == "delete_rows":
        cells = (
            cell
            for row in sheet.iter_rows(
                min_row=request.index,
                max_row=end,
                min_col=1,
                max_col=sheet.max_column,
            )
            for cell in row
        )
    else:
        cells = (
            cell
            for row in sheet.iter_rows(
                min_row=1,
                max_row=sheet.max_row,
                min_col=request.index,
                max_col=end,
            )
            for cell in row
        )
    return sum(cell.value is not None for cell in cells)


def _validated_new_sheet_name(book, raw_name: str | None) -> str:
    name = (raw_name or "").strip()
    if (
        not name
        or len(name) > 31
        or _INVALID_SHEET_NAME.search(name)
        or name.casefold() in {existing.casefold() for existing in book.sheetnames}
    ):
        raise WorkbookStructureError("invalid_sheet_name")
    return name


def apply_structure_edit(
    source: Path,
    destination: Path,
    sheet_map: Mapping[str, str],
    request: SpreadsheetStructureEditRequest,
) -> StructureChange:
    """Apply exactly one safe structural operation and verify the saved workbook."""
    if source.suffix.lower() != ".xlsx" or destination.suffix.lower() != ".xlsx":
        raise WorkbookStructureError("unsupported_workbook_format")

    book = _load(source)
    try:
        before_sheet_names = tuple(book.sheetnames)
        if request.action == "add_sheet":
            new_sheet_name = _validated_new_sheet_name(book, request.new_sheet_name)
            book.create_sheet(new_sheet_name)
            change = StructureChange(
                action=request.action,
                sheet_key=None,
                sheet_name=None,
                index=None,
                count=1,
                new_sheet_name=new_sheet_name,
                affected_nonempty_cells=0,
            )
        else:
            assert request.sheet_key is not None
            sheet_name = sheet_map.get(request.sheet_key)
            if sheet_name is None or sheet_name not in book.sheetnames:
                raise WorkbookStructureError("unknown_sheet")
            sheet = book[sheet_name]
            _validate_row_or_column_edit(book, sheet)
            _validate_index(sheet, request)
            affected = 0
            if request.action.startswith("delete_"):
                affected = _nonempty_delete_count(sheet, request)
                if affected and not request.confirm_nonempty:
                    raise WorkbookStructureError(
                        "nonempty_delete_requires_confirmation"
                    )
            assert request.index is not None
            operations = {
                "insert_rows": sheet.insert_rows,
                "delete_rows": sheet.delete_rows,
                "insert_columns": sheet.insert_cols,
                "delete_columns": sheet.delete_cols,
            }
            operations[request.action](request.index, request.count)
            change = StructureChange(
                action=request.action,
                sheet_key=request.sheet_key,
                sheet_name=sheet_name,
                index=request.index,
                count=request.count,
                new_sheet_name=None,
                affected_nonempty_cells=affected,
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        book.save(destination)
        reopened = _load(destination)
        expected_names = (
            before_sheet_names + (change.new_sheet_name,)
            if change.action == "add_sheet"
            else before_sheet_names
        )
        if tuple(reopened.sheetnames) != expected_names:
            raise WorkbookStructureError("workbook_validation_failed")
    except WorkbookStructureError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise WorkbookStructureError("workbook_validation_failed") from exc
    return change
