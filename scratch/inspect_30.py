import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402

print("users db:", settings.users_db_path)
conn = sqlite3.connect(settings.users_db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "select name from sqlite_master where type='table' and "
    "(name like '%patch%' or name like '%candidate%' or name like '%evolution%')"
).fetchall()
tables = [r[0] for r in rows]
print("tables:", tables)
for t in tables:
    print(" ", t, conn.execute(f"select count(*) from {t}").fetchone()[0])
if "evolution_patches" in tables:
    cols = [r[1] for r in conn.execute("pragma table_info(evolution_patches)")]
    print("evolution_patches cols:", cols)
conn.close()
