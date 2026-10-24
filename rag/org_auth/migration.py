from __future__ import annotations

import json
from dataclasses import dataclass

from rag.kb_lifecycle import assert_kb_available_in_tx
from rag.org_auth.repository import OrgRepository

_UNMIGRATED_SQL = """
    SELECT m.kb_id,m.owner_id,m.scope FROM kb_metadata m
    LEFT JOIN kb_org_policies p ON p.kb_id=m.kb_id WHERE p.kb_id IS NULL
"""


@dataclass(frozen=True)
class MigrationReport:
    migrated: int
    unmigrated: int = 0


def count_unmigrated_knowledge_bases(db) -> int:
    """Return how many knowledge bases still lack an organization policy.

    The startup migration runs on every launch, so a non-zero counter means a
    knowledge base was created between the migration transaction and the
    self-check (or the write failed); those knowledge bases stay owner and
    administrator only, which is the documented migration posture.
    """
    with db.locked_read() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM ({_UNMIGRATED_SQL})").fetchone()[0])


def migrate_legacy_knowledge_bases(db) -> MigrationReport:
    OrgRepository(db).install_schema()
    migrated = 0
    with db.locked_transaction(immediate=True) as conn:
        rows = conn.execute(_UNMIGRATED_SQL).fetchall()
        for row in rows:
            assert_kb_available_in_tx(conn, row["kb_id"])
            conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type,sensitivity) VALUES(?,NULL,'unassigned',NULL)", (row["kb_id"],))
            shares = [dict(r) for r in conn.execute("SELECT user_id,permission,granted_by FROM kb_shares WHERE kb_id=?", (row["kb_id"],))]
            conn.execute("INSERT INTO org_audit_events(actor_id,action,target_type,target_id,result,reason,details_json) VALUES(NULL,'legacy_kb_migrated','knowledge_base',?,'success','unassigned',?)", (row["kb_id"], json.dumps({"owner_id": row["owner_id"], "scope": row["scope"], "shares": shares}, ensure_ascii=False)))
            migrated += 1
        unmigrated = int(
            conn.execute(f"SELECT COUNT(*) FROM ({_UNMIGRATED_SQL})").fetchone()[0]
        )
    return MigrationReport(migrated=migrated, unmigrated=unmigrated)
