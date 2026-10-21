import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect("data/users.db")
print("== dataset 9 ==")
for row in conn.execute(
    "select id, document_id, document_version_id, format, status, sheet_count, total_rows, "
    "total_columns, last_error_code from structured_datasets where id = 9"
):
    print(" ", row)
print("  document:", list(conn.execute(
    "select id, doc_name, kb_id from kb_documents where id = (select document_id from structured_datasets where id=9)"
)))
conn.close()

conn = sqlite3.connect("data/evolution.db")
print("\n== candidate_testing ==")
for row in conn.execute(
    "select id, cluster_id, artifact_type, target, status, confidence, created_at, updated_at "
    "from candidates where status = 'candidate_testing'"
):
    print(" ", row)
print("\n== clusters ==")
for row in conn.execute("select id, kb_id, feature, status from failure_clusters order by id"):
    print(" ", row)
conn.close()
