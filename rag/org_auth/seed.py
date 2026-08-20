from __future__ import annotations

import argparse
from dataclasses import dataclass

from rag.auth import hash_password
from rag.org_auth.repository import OrgRepository
from rag.org_auth.seed_manifest import build_seed_manifest
from rag.user_db import UserDB


@dataclass(frozen=True)
class SeedReport:
    created_people: int
    created_units: int
    created_knowledge_bases: int


def seed_organization(db: UserDB, metadata_only: bool = False) -> SeedReport:
    manifest = build_seed_manifest()
    repo = OrgRepository(db)
    repo.install_schema()
    created_people = created_units = created_kbs = 0
    with db.locked_transaction(immediate=True) as conn:
        for u in manifest.units:
            cur = conn.execute("INSERT OR IGNORE INTO org_units(code,parent_code,name,kind) VALUES(?,?,?,?)", (u.code, u.parent_code, u.name, u.kind))
            created_units += cur.rowcount
        for p in manifest.people:
            existing = conn.execute("SELECT id FROM users WHERE username=?", (p.username,)).fetchone()
            if existing is None:
                cur = conn.execute("INSERT INTO users(username,salt,password,is_admin) VALUES(?,?,?,?)", (p.username, "", hash_password("123456"), int(p.is_admin)))
                created_people += 1
                user_id = cur.lastrowid
            else:
                user_id = existing[0]
            conn.execute("INSERT OR IGNORE INTO org_people(person_code,user_id,display_name,job_title,seed_key) VALUES(?,?,?,?,?)", (p.person_code, user_id, p.display_name, p.job_title, p.person_code))
        conn.executemany("INSERT OR IGNORE INTO org_memberships(person_code,org_code,is_primary) VALUES(?,?,?)", ((m.person_code, m.org_code, int(m.is_primary)) for m in manifest.memberships))
        conn.executemany("INSERT OR IGNORE INTO org_manager_scopes(person_code,org_code) VALUES(?,?)", ((m.person_code, m.org_code) for m in manifest.manager_scopes))
        conn.executemany("INSERT OR IGNORE INTO org_sensitive_qualifications(person_code,sensitivity) VALUES(?,?)", manifest.qualifications)
        for kb in manifest.knowledge_bases:
            cur = conn.execute("INSERT OR IGNORE INTO kb_org_policies(kb_id,org_code,kb_type,sensitivity,seed_key) VALUES(?,?,?,?,?)", (kb.kb_id, kb.org_code, kb.kb_type, kb.sensitivity, kb.kb_id))
            created_kbs += cur.rowcount
            conn.execute("INSERT OR IGNORE INTO kb_metadata(kb_id,name,owner_id,scope) VALUES(?,?,0,'private')", (kb.kb_id, kb.name))
        conn.execute("INSERT INTO org_seed_state(seed_key,seed_version,status) VALUES('fictional-org',1,'complete') ON CONFLICT(seed_key) DO UPDATE SET status='complete',updated_at=strftime('%s','now')")
    return SeedReport(created_people, created_units, created_kbs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    db = UserDB(args.db_path)
    try:
        report = seed_organization(db, metadata_only=args.metadata_only)
        print(f"units={report.created_units} people={report.created_people} knowledge_bases={report.created_knowledge_bases}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
