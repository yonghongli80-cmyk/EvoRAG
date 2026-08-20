from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from rag.org_auth.schema import install_org_schema


class OrgRepository:
    def __init__(self, db):
        self.db = db

    def install_schema(self) -> None:
        with self.db.locked_transaction() as conn:
            install_org_schema(conn)

    def table_names(self) -> set[str]:
        with self.db.locked_read() as conn:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def create_unit(self, code: str, parent_code: str | None, name: str, kind: str) -> None:
        with self.db.locked_transaction() as conn:
            conn.execute("INSERT INTO org_units(code,parent_code,name,kind) VALUES(?,?,?,?)", (code, parent_code, name, kind))

    def create_units(self, rows: Iterable[tuple[str, str | None, str, str]]) -> None:
        with self.db.locked_transaction() as conn:
            conn.executemany("INSERT INTO org_units(code,parent_code,name,kind) VALUES(?,?,?,?)", rows)

    def get_unit(self, code: str) -> dict | None:
        with self.db.locked_read() as conn:
            row = conn.execute("SELECT * FROM org_units WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def descendants(self, code: str) -> set[str]:
        with self.db.locked_read() as conn:
            rows = conn.execute("""
                WITH RECURSIVE tree(code) AS (
                  SELECT code FROM org_units WHERE code=? AND active=1
                  UNION ALL SELECT u.code FROM org_units u JOIN tree t ON u.parent_code=t.code WHERE u.active=1
                ) SELECT code FROM tree
            """, (code,)).fetchall()
        return {r[0] for r in rows}

    def get_person_for_user(self, user_id: int) -> dict | None:
        with self.db.locked_read() as conn:
            row = conn.execute("SELECT * FROM org_people WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def memberships(self, person_code: str) -> list[dict]:
        with self.db.locked_read() as conn:
            rows = conn.execute("SELECT m.*,u.name,u.active FROM org_memberships m JOIN org_units u ON u.code=m.org_code WHERE person_code=?", (person_code,)).fetchall()
        return [dict(r) for r in rows]

    def manager_scopes(self, person_code: str) -> set[str]:
        with self.db.locked_read() as conn:
            rows = conn.execute("SELECT org_code FROM org_manager_scopes WHERE person_code=?", (person_code,)).fetchall()
        return {r[0] for r in rows}

    def qualifications(self, person_code: str) -> set[str]:
        with self.db.locked_read() as conn:
            rows = conn.execute("SELECT sensitivity FROM org_sensitive_qualifications WHERE person_code=?", (person_code,)).fetchall()
        return {r[0] for r in rows}

    def get_kb_policy(self, kb_id: str) -> dict | None:
        with self.db.locked_read() as conn:
            row = conn.execute("SELECT * FROM kb_org_policies WHERE kb_id=?", (kb_id,)).fetchone()
        return dict(row) if row else None

    def grants(self, kb_id: str) -> list[dict]:
        with self.db.locked_read() as conn:
            rows = conn.execute("SELECT * FROM kb_org_grants WHERE kb_id=? AND active=1", (kb_id,)).fetchall()
        return [dict(r) for r in rows]
