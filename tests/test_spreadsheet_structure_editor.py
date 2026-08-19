from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill

from rag.spreadsheets.edit_models import SpreadsheetStructureEditRequest


def _save_workbook(path: Path) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "Data"
    sheet["A1"] = "header"
    sheet["A2"] = "first"
    sheet["B2"] = "second"
    sheet["A3"] = "third"
    sheet["A2"].fill = PatternFill(fill_type="solid", fgColor="FFFF00")
    book.create_sheet("Other")["A1"] = "untouched"
    book.save(path)


def _apply(source: Path, destination: Path, **values):
    from rag.spreadsheets.structure_editor import apply_structure_edit

    request = SpreadsheetStructureEditRequest(base_version_id=1, **values)
    return apply_structure_edit(
        source,
        destination,
        {"sheet_0001": "Data", "sheet_0002": "Other"},
        request,
    )


def test_insert_rows_shifts_values_and_styles_without_touching_other_sheet(tmp_path):
    source = tmp_path / "source.xlsx"
    destination = tmp_path / "edited.xlsx"
    _save_workbook(source)

    change = _apply(
        source,
        destination,
        action="insert_rows",
        sheet_key="sheet_0001",
        index=2,
        count=1,
    )

    book = load_workbook(destination)
    assert book["Data"]["A2"].value is None
    assert book["Data"]["A3"].value == "first"
    assert book["Data"]["A3"].fill.fgColor.rgb == "00FFFF00"
    assert book["Other"]["A1"].value == "untouched"
    assert change.action == "insert_rows"
    assert change.affected_nonempty_cells == 0


def test_insert_columns_shifts_existing_cells(tmp_path):
    source = tmp_path / "source.xlsx"
    destination = tmp_path / "edited.xlsx"
    _save_workbook(source)

    _apply(
        source,
        destination,
        action="insert_columns",
        sheet_key="sheet_0001",
        index=2,
        count=2,
    )

    book = load_workbook(destination)
    assert book["Data"]["B2"].value is None
    assert book["Data"]["D2"].value == "second"


def test_insert_rows_accepts_workbook_without_security_metadata(tmp_path):
    source = tmp_path / "source.xlsx"
    destination = tmp_path / "edited.xlsx"
    _save_workbook(source)

    book = load_workbook(source)
    book.security = None
    book.save(source)
    assert load_workbook(source).security is None

    _apply(
        source,
        destination,
        action="insert_rows",
        sheet_key="sheet_0001",
        index=1,
        count=1,
    )

    assert load_workbook(destination)["Data"]["A2"].value == "header"


@pytest.mark.parametrize(
    ("action", "index", "count", "expected_count"),
    [
        ("delete_rows", 2, 1, 2),
        ("delete_columns", 1, 1, 3),
    ],
)
def test_nonempty_delete_requires_confirmation(
    tmp_path, action, index, count, expected_count
):
    from rag.spreadsheets.structure_editor import WorkbookStructureError

    source = tmp_path / "source.xlsx"
    destination = tmp_path / "edited.xlsx"
    _save_workbook(source)

    with pytest.raises(WorkbookStructureError) as caught:
        _apply(
            source,
            destination,
            action=action,
            sheet_key="sheet_0001",
            index=index,
            count=count,
        )
    assert caught.value.reason_code == "nonempty_delete_requires_confirmation"
    assert not destination.exists()

    change = _apply(
        source,
        destination,
        action=action,
        sheet_key="sheet_0001",
        index=index,
        count=count,
        confirm_nonempty=True,
    )
    assert change.affected_nonempty_cells == expected_count


def test_confirmed_delete_rows_shifts_following_values(tmp_path):
    source = tmp_path / "source.xlsx"
    destination = tmp_path / "edited.xlsx"
    _save_workbook(source)

    _apply(
        source,
        destination,
        action="delete_rows",
        sheet_key="sheet_0001",
        index=2,
        confirm_nonempty=True,
    )

    assert load_workbook(destination)["Data"]["A2"].value == "third"


def test_add_sheet_appends_a_blank_named_sheet(tmp_path):
    source = tmp_path / "source.xlsx"
    destination = tmp_path / "edited.xlsx"
    _save_workbook(source)

    change = _apply(
        source,
        destination,
        action="add_sheet",
        new_sheet_name="August",
    )

    book = load_workbook(destination)
    assert book.sheetnames == ["Data", "Other", "August"]
    assert book["August"].max_row == 1
    assert book["August"]["A1"].value is None
    assert change.new_sheet_name == "August"


@pytest.mark.parametrize("new_name", ["data", "bad/name"])
def test_add_sheet_rejects_duplicate_or_illegal_names(tmp_path, new_name):
    from rag.spreadsheets.structure_editor import WorkbookStructureError

    source = tmp_path / "source.xlsx"
    destination = tmp_path / "edited.xlsx"
    _save_workbook(source)

    with pytest.raises(WorkbookStructureError) as caught:
        _apply(source, destination, action="add_sheet", new_sheet_name=new_name)
    assert caught.value.reason_code == "invalid_sheet_name"
    assert not destination.exists()


@pytest.mark.parametrize("unsafe_kind", ["formula", "merge", "protection"])
def test_row_operations_refuse_unsafe_workbooks(tmp_path, unsafe_kind):
    from rag.spreadsheets.structure_editor import WorkbookStructureError

    source = tmp_path / "source.xlsx"
    destination = tmp_path / "edited.xlsx"
    _save_workbook(source)
    book = load_workbook(source)
    if unsafe_kind == "formula":
        book["Other"]["B1"] = "=1+1"
        expected = "workbook_contains_formulas"
    elif unsafe_kind == "merge":
        book["Data"].merge_cells("A2:B2")
        expected = "sheet_contains_merged_cells"
    else:
        book["Data"].protection.sheet = True
        expected = "protected_workbook"
    book.save(source)

    with pytest.raises(WorkbookStructureError) as caught:
        _apply(
            source,
            destination,
            action="insert_rows",
            sheet_key="sheet_0001",
            index=2,
        )
    assert caught.value.reason_code == expected
    assert not destination.exists()
