import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

c = sqlite3.connect("data/users.db")
for (n,) in c.execute("select name from sqlite_master where type='table' order by name"):
    print(n)
c.close()
