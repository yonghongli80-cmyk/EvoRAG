import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

c = sqlite3.connect("data/evolution.db")
c.row_factory = sqlite3.Row
print("=== evolution_candidate_audit columns ===")
print([r[1] for r in c.execute("pragma table_info(evolution_candidate_audit)")])
print()
for r in c.execute(
    "select * from evolution_candidate_audit where candidate_id = 30 order by rowid"
):
    print(dict(r))
print()
print("=== status of all candidates ===")
for r in c.execute("select id, cluster_id, artifact_type, target, status from candidates order by id"):
    print(dict(r))
c.close()
