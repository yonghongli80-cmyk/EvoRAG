"""Verify the schema-mirror backfill landed in the production store and reaches the catalog."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from rag.spreadsheets.catalog import build_authorized_catalog  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

conn = sqlite3.connect(ROOT / "data" / "users.db")
for table in ("structured_datasets", "structured_sheets", "structured_columns"):
    count = conn.execute(f"select count(*) from {table}").fetchone()[0]
    print(f"{table:22s} rows={count}")
print("sheets per dataset:", list(conn.execute(
    "select dataset_id, count(*) from structured_sheets group by dataset_id order by dataset_id"
)))
conn.close()

user_db = UserDB(settings.users_db_path)
try:
    user = user_db.get_user_by_id(1)
    for kb_id in ("kb_accessories", "kb_evolab", "kb_hr"):
        catalog = build_authorized_catalog(
            user_db, Path(settings.structured_datasets_root), kb_id, user
        )
        sheets = sum(len(dataset.sheets) for dataset in catalog.datasets)
        columns = sum(len(sheet.columns) for dataset in catalog.datasets for sheet in dataset.sheets)
        print(f"\n{kb_id}: datasets={len(catalog.datasets)} sheets={sheets} columns={columns}")
        for dataset in catalog.datasets:
            keys = [column.column_key for sheet in dataset.sheets for column in sheet.columns][:4]
            print(f"  dataset={dataset.dataset_id} "
                  f"sheets={len(dataset.sheets)} sample_keys={keys}")
finally:
    user_db.close()
