import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect("data/evolution.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "select id, question, details_jev_question_set_version v, details_decision_metadata m, created_at "
    "from failure_observations order by id desc limit 6"
).fetchall()
for row in rows:
    print("id=", row["id"], "|", row["question"], "| version=", row["v"])
    print("   metadata:", (row["m"] or "")[:800])
conn.close()
