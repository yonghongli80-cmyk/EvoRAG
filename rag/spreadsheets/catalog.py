"""Authorized schema catalog for current spreadsheet datasets."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from rag.api_security import require_doc_view
from rag.user_db import UserDB


@dataclass(frozen=True)
class CatalogColumn:
    column_key: str
    display_name: str
    normalized_name: str
    inferred_type: str
    unit_hint: str | None = None
    samples: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogSheet:
    sheet_key: str
    name: str
    row_count: int
    columns: tuple[CatalogColumn, ...]


@dataclass(frozen=True)
class CatalogDataset:
    dataset_id: int
    document_id: int
    version_id: int
    document_name: str
    path: Path
    sheets: tuple[CatalogSheet, ...]


@dataclass(frozen=True)
class SpreadsheetCatalog:
    kb_id: str
    datasets: tuple[CatalogDataset, ...]


def build_authorized_catalog(db: UserDB, datasets_root: Path, kb_id: str, user: dict) -> SpreadsheetCatalog:
    root = Path(datasets_root).resolve()
    datasets = []
    for item in db.list_current_ready_spreadsheet_datasets(kb_id):
        try:
            require_doc_view(db, item["doc_name"], kb_id, user)
        except Exception:
            continue
        path = (root / item["storage_key"]).resolve()
        if root != path and root not in path.parents:
            continue
        grouped: dict[str, dict] = {}
        for row in db.list_structured_dataset_schema(int(item["id"])):
            sheet = grouped.setdefault(row["sheet_key"], {"name": row["sheet_name"], "rows": row["row_count"], "columns": []})
            try:
                samples = tuple(str(value) for value in json.loads(row["sample_values_json"] or "[]")[:5])
            except (TypeError, ValueError, json.JSONDecodeError):
                samples = ()
            sheet["columns"].append(CatalogColumn(row["column_key"], row["display_name"], row["normalized_name"], row["inferred_type"], row["unit"] or None, samples))
        sheets = tuple(CatalogSheet(key, value["name"], value["rows"], tuple(value["columns"])) for key, value in grouped.items())
        datasets.append(CatalogDataset(int(item["id"]), int(item["document_id"]), int(item["document_version_id"]), item["doc_name"], path, sheets))
    return SpreadsheetCatalog(kb_id, tuple(datasets))
