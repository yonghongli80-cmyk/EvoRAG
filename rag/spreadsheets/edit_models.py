"""Strict contracts for spreadsheet cell edits and immutable restores."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

type CellValueType = Literal["text", "number", "date", "boolean", "blank"]
type SpreadsheetEditOperation = Literal["edit", "rollback"]
type SpreadsheetStructureAction = Literal[
    "insert_rows",
    "delete_rows",
    "insert_columns",
    "delete_columns",
    "add_sheet",
]
type SpreadsheetEditStatus = Literal[
    "pending", "active", "failed", "cancelled", "rejected"
]


class CellPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sheet_key: str = Field(min_length=1, max_length=200)
    cell: str = Field(min_length=1, max_length=20)
    value_type: CellValueType
    value: object | None

    @model_validator(mode="after")
    def validate_typed_value(self) -> CellPatch:
        value = self.value
        if self.value_type == "blank":
            valid = value is None
        elif self.value_type == "text":
            valid = type(value) is str
        elif self.value_type == "number":
            valid = type(value) in {int, float} and math.isfinite(value)
        elif self.value_type == "boolean":
            valid = type(value) is bool
        else:
            valid = type(value) is str and self._is_iso_date(value)
        if not valid:
            raise ValueError(f"value does not match {self.value_type}")
        return self

    @staticmethod
    def _is_iso_date(value: str) -> bool:
        try:
            if "T" in value or " " in value:
                datetime.fromisoformat(value)
            else:
                date.fromisoformat(value)
        except ValueError:
            return False
        return True


class SpreadsheetEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_version_id: int = Field(ge=1)
    changes: tuple[CellPatch, ...] = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_unique_cells(self) -> SpreadsheetEditRequest:
        keys = [
            (change.sheet_key.casefold(), change.cell.upper())
            for change in self.changes
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate spreadsheet cell patch")
        return self


class SpreadsheetRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_version_id: int = Field(ge=1)
    reason: str = Field(default="", max_length=500)


class SpreadsheetStructureEditRequest(BaseModel):
    """One atomic workbook-structure operation against a known version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_version_id: int = Field(ge=1)
    action: SpreadsheetStructureAction
    sheet_key: str | None = Field(default=None, min_length=1, max_length=200)
    index: int | None = Field(default=None, ge=1)
    count: int = Field(default=1, ge=1, le=100)
    new_sheet_name: str | None = Field(default=None, min_length=1, max_length=31)
    confirm_nonempty: bool = False
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_action_fields(self) -> SpreadsheetStructureEditRequest:
        if self.action == "add_sheet":
            if self.new_sheet_name is None or not self.new_sheet_name.strip():
                raise ValueError("add_sheet requires new_sheet_name")
            if self.sheet_key is not None or self.index is not None:
                raise ValueError("add_sheet does not accept a sheet target")
            return self

        if self.sheet_key is None or self.index is None:
            raise ValueError("row and column actions require sheet_key and index")
        if self.new_sheet_name is not None:
            raise ValueError("row and column actions do not accept new_sheet_name")
        return self


class SpreadsheetEditSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    document_id: int
    base_version_id: int
    new_version_id: int | None
    actor_user_id: int
    operation: SpreadsheetEditOperation
    status: SpreadsheetEditStatus
    change_count: int
    restored_from_version_id: int | None = None


class SpreadsheetEditAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["pending"] = "pending"
    session_id: int
    version_id: int


class SpreadsheetEditUnchanged(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["unchanged"] = "unchanged"


class SpreadsheetEditStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: int
    version_id: int | None
    status: SpreadsheetEditStatus
    error_code: str = ""


class RequestMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ip_address: str = Field(default="", max_length=200)
    user_agent: str = Field(default="", max_length=1000)
