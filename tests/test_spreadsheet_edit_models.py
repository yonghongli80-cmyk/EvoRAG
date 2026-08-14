import pytest
from pydantic import ValidationError


def test_cell_patch_accepts_a_typed_number_value():
    from rag.spreadsheets.edit_models import CellPatch

    patch = CellPatch(
        sheet_key="sheet_0001",
        cell="B12",
        value_type="number",
        value=6500,
    )

    assert patch.value == 6500


def test_cell_patch_rejects_a_string_for_number_type():
    from rag.spreadsheets.edit_models import CellPatch

    with pytest.raises(ValidationError):
        CellPatch(
            sheet_key="sheet_0001",
            cell="B12",
            value_type="number",
            value="6500",
        )


def test_cell_patch_requires_none_for_blank_and_forbids_extra_fields():
    from rag.spreadsheets.edit_models import CellPatch

    with pytest.raises(ValidationError):
        CellPatch(
            sheet_key="sheet_0001",
            cell="B12",
            value_type="blank",
            value="",
        )
    with pytest.raises(ValidationError):
        CellPatch(
            sheet_key="sheet_0001",
            cell="B12",
            value_type="blank",
            value=None,
            formula="=SUM(A1:A2)",
        )


def test_edit_request_enforces_the_500_unique_patch_limit():
    from rag.spreadsheets.edit_models import SpreadsheetEditRequest

    changes = tuple(
        {
            "sheet_key": "sheet_0001",
            "cell": f"A{row}",
            "value_type": "number",
            "value": row,
        }
        for row in range(1, 502)
    )

    with pytest.raises(ValidationError):
        SpreadsheetEditRequest(base_version_id=1, changes=changes)


def test_edit_request_rejects_duplicate_sheet_and_cell_pairs():
    from rag.spreadsheets.edit_models import SpreadsheetEditRequest

    change = {
        "sheet_key": "sheet_0001",
        "cell": "B12",
        "value_type": "number",
        "value": 6500,
    }

    with pytest.raises(ValidationError):
        SpreadsheetEditRequest(
            base_version_id=1,
            changes=(change, change),
        )
