import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

c = sqlite3.connect("data/evolution.db")
c.row_factory = sqlite3.Row
row = c.execute("select * from candidates where id = 30").fetchone()
if row is None:
    print("candidate 30 not found")
    raise SystemExit(1)
for key in row:
    if key == "payload":
        continue
    print(f"{key}: {row[key]}")
print("\n=== payload (truncated) ===")
payload = row["payload"] or ""
try:
    data = json.loads(payload)
    print("payload type:", type(data).__name__, "keys:", list(data) if isinstance(data, dict) else len(data))
    text = json.dumps(data, ensure_ascii=False, indent=2)
except Exception:  # noqa: BLE001
    text = payload
print(text[:6000])
print("\n=== audit ===")
for r in c.execute(
    "select id, action, detail, status, created_at from evolution_candidate_audit "
    "where candidate_id = 30 order by id"
):
    print(dict(r))
c.close()
