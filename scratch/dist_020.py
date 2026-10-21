"""How often does real-020 end at a structured clarification vs an evidence refusal?

Reads the recorded observations (the same rows the evolution loop diagnoses) so the answer
is history, not one lucky run.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect("data/evolution.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "select id, question, route, details_answerability_kind kind, "
    "details_structured_query_kind sque, details_decision_metadata meta, total_ms "
    "from failure_observations where question = ? order by id",
    ("找一下副厂配件明细",),
).fetchall()
print(f"observations for the question: {len(rows)}")
print("shape (route / answerability / structured_kind):")
for (route, kind, sque), count in Counter(
    (r["route"], r["kind"], r["sque"]) for r in rows
).most_common():
    print(f"  {count:3d}x  route={route!s:12s} answerability={kind!s:24s} structured={sque}")
print()
for row in rows[-6:]:
    meta = json.loads(row["meta"] or "{}")
    print(
        f"  obs#{row['id']} route={row['route']} kind={row['kind']} sque={row['sque']} "
        f"jev_route={meta.get('jev_route')} reason={meta.get('route_reason')} "
        f"evidence={meta.get('passage_evidence')}/{meta.get('passage_judged')} "
        f"{row['total_ms']:.0f}ms"
    )
conn.close()
