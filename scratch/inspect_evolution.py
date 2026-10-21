import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

c = sqlite3.connect("data/evolution.db")
c.row_factory = sqlite3.Row
print("tables:")
for (n,) in c.execute("select name from sqlite_master where type='table' order by name"):
    cnt = c.execute(f"select count(*) from {n}").fetchone()[0]
    print(f"  {n:40s} {cnt}")
print()
for n in ("candidates", "evolution_candidates", "patch_candidates"):
    try:
        cols = [r[1] for r in c.execute(f"pragma table_info({n})")]
        print(n, "cols:", cols)
    except Exception as exc:  # noqa: BLE001
        print(n, "missing", exc)
c.close()
