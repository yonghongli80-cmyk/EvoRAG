"""Collect the numbers the handoff cites, so none of them are copied from memory."""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402

print("== structured mirror (users.db) ==")
conn = sqlite3.connect(settings.users_db_path)
for table in ("structured_datasets", "structured_sheets", "structured_columns"):
    print(f"  {table}: {conn.execute(f'select count(*) from {table}').fetchone()[0]}")
print("  per-dataset sheet/column counts:")
for dataset_id, sheets, cols in conn.execute(
    "select d.id, count(distinct s.id), count(c.id) from structured_datasets d "
    "left join structured_sheets s on s.dataset_id = d.id "
    "left join structured_columns c on c.sheet_id = s.id group by d.id order by d.id"
):
    print(f"    dataset {dataset_id}: sheets={sheets} columns={cols}")
conn.close()

print("\n== evolution (evolution.db) ==")
conn = sqlite3.connect(settings.evolution_db_path)
conn.row_factory = sqlite3.Row
status = Counter(
    row[0] for row in conn.execute("select status from candidates")
)
print("  candidates:", dict(status), f"total={sum(status.values())}")
print("  canary candidates:", [
    dict(row) for row in conn.execute(
        "select id, status, target, json_extract(gate_log, '$[2].detail') as canary "
        "from candidates where status in ('canary','stable')"
    )
])
obs = conn.execute("select count(*) from failure_observations").fetchone()[0]
kinds = Counter(
    row[0] for row in conn.execute("select details_answerability_kind from failure_observations")
)
sque = Counter(
    row[0] for row in conn.execute("select details_structured_query_kind from failure_observations")
)
print(f"  observations: {obs}")
print("  answerability:", dict(kinds))
print("  structured_query_kind:", dict(sque))
print("  clusters:", conn.execute("select count(*) from failure_clusters").fetchone()[0])
print("  gap tickets:", conn.execute("select count(*) from knowledge_gap_tickets").fetchone()[0])
conn.close()

print("\n== settings that matter ==")
for name in (
    "jev_mode", "jev_vote_rounds", "jev_route_confidence", "reliability_min_score",
    "rerank_provider", "rerank_top_k", "evolution_enabled", "evolution_candidate_enabled",
    "evolution_candidate_canary_percent",
):
    print(f"  {name} = {getattr(settings, name, '<missing>')}")
