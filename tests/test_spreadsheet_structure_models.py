import pytest
from pydantic import ValidationError

import rag.spreadsheets.edit_models as models


def _model():
    return models.SpreadsheetStructureEditRequest


@pytest.mark.parametrize(
    "action",
    ["insert_rows", "delete_rows", "insert_columns", "delete_columns"],
)
def test_row_and_column_actions_require_a_bounded_target(action):
    request = _model()(
        base_version_id=7,
        action=action,
        sheet_key="sheet_0001",
        index=3,
        count=100,
        reason="structure cleanup",
    )
    assert request.count == 100

    with pytest.raises(ValidationError):
        _model()(base_version_id=7, action=action, index=3)
    with pytest.raises(ValidationError):
        _model()(
            base_version_id=7,
            action=action,
            sheet_key="sheet_0001",
            index=3,
            count=101,
        )


def test_add_sheet_requires_only_a_new_name():
    request = _model()(
        base_version_id=7,
        action="add_sheet",
        new_sheet_name="August",
    )
    assert request.new_sheet_name == "August"

    with pytest.raises(ValidationError):
        _model()(base_version_id=7, action="add_sheet")
    with pytest.raises(ValidationError):
        _model()(
            base_version_id=7,
            action="add_sheet",
            new_sheet_name="August",
            sheet_key="sheet_0001",
            index=1,
        )


def test_row_action_forbids_new_sheet_name():
    with pytest.raises(ValidationError):
        _model()(
            base_version_id=7,
            action="insert_rows",
            sheet_key="sheet_0001",
            index=1,
            new_sheet_name="Unexpected",
        )
