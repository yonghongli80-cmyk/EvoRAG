"""Stable vocabulary for spreadsheet dataset metadata."""

from enum import StrEnum


class DatasetStatus(StrEnum):
    STAGING = "staging"
    READY = "ready"
    FAILED = "failed"
    HISTORICAL = "historical"


class SemanticIndexMode(StrEnum):
    SCHEMA_ONLY = "schema_only"
    ROW_GROUPS = "row_groups"


class SheetVisibility(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"
    VERY_HIDDEN = "very_hidden"


class ColumnType(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
