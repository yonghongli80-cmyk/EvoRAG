from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rag.auth import hash_password
from rag.kb_lifecycle import assert_kb_available_in_tx
from rag.org_auth.migration import migrate_legacy_knowledge_bases
from rag.org_auth.repository import OrgRepository
from rag.org_auth.seed_manifest import build_seed_manifest
from rag.user_db import UserDB


@dataclass(frozen=True)
class SeedReport:
    created_people: int
    created_units: int
    created_knowledge_bases: int


def seed_organization(
    db: UserDB,
    metadata_only: bool = False,
    index_document: Callable[[str, str, str, int], int] | None = None,
    documents_root: Path | None = None,
) -> SeedReport:
    manifest = build_seed_manifest()
    repo = OrgRepository(db)
    repo.install_schema()
    with db.locked_transaction() as conn:
        prior_seed = conn.execute(
            "SELECT 1 FROM org_seed_state WHERE seed_key='fictional-org' AND status='complete'"
        ).fetchone()
        if prior_seed:
            conn.executemany(
                "UPDATE org_units SET seed_key=? WHERE code=? AND seed_key IS NULL",
                ((unit.code, unit.code) for unit in manifest.units),
            )
    with db.locked_read() as conn:
        collisions = []
        for unit in manifest.units:
            row = conn.execute(
                "SELECT seed_key FROM org_units WHERE code=? OR seed_key=?",
                (unit.code, unit.code),
            ).fetchone()
            if row is not None and row["seed_key"] != unit.code:
                collisions.append(f"org_unit:{unit.code}")
        for person in manifest.people:
            by_code = conn.execute(
                "SELECT p.seed_key,u.username FROM org_people p "
                "JOIN users u ON u.id=p.user_id WHERE p.person_code=? OR p.seed_key=?",
                (person.person_code, person.person_code),
            ).fetchone()
            if by_code is not None and not (
                by_code["seed_key"] == person.person_code
                and by_code["username"] == person.username
            ):
                collisions.append(f"person:{person.person_code}")
            row = conn.execute(
                "SELECT u.id,p.seed_key,p.person_code FROM users u "
                "LEFT JOIN org_people p ON p.user_id=u.id WHERE u.username=?",
                (person.username,),
            ).fetchone()
            if row is not None and not (
                row["seed_key"] == person.person_code
                and row["person_code"] == person.person_code
            ):
                collisions.append(f"username:{person.username}")
        for kb in manifest.knowledge_bases:
            row = conn.execute(
                "SELECT m.kb_id,p.seed_key FROM kb_metadata m "
                "LEFT JOIN kb_org_policies p ON p.kb_id=m.kb_id WHERE m.kb_id=?",
                (kb.kb_id,),
            ).fetchone()
            if row is not None and row["seed_key"] != kb.kb_id:
                collisions.append(f"knowledge_base:{kb.kb_id}")
    if collisions:
        raise ValueError("seed collision: " + ", ".join(collisions[:10]))
    migrate_legacy_knowledge_bases(db)
    created_people = created_units = created_kbs = 0
    admin_user_id = 0
    with db.locked_transaction(immediate=True) as conn:
        for u in manifest.units:
            cur = conn.execute("INSERT OR IGNORE INTO org_units(code,parent_code,name,kind,seed_key) VALUES(?,?,?,?,?)", (u.code, u.parent_code, u.name, u.kind, u.code))
            created_units += cur.rowcount
        for p in manifest.people:
            existing = conn.execute("SELECT id FROM users WHERE username=?", (p.username,)).fetchone()
            if existing is None:
                cur = conn.execute("INSERT INTO users(username,salt,password,is_admin) VALUES(?,?,?,?)", (p.username, "", hash_password("123456"), int(p.is_admin)))
                created_people += 1
                user_id = cur.lastrowid
            else:
                user_id = existing[0]
            if p.is_admin and admin_user_id == 0:
                admin_user_id = int(user_id)
            conn.execute("INSERT OR IGNORE INTO org_people(person_code,user_id,display_name,job_title,seed_key) VALUES(?,?,?,?,?)", (p.person_code, user_id, p.display_name, p.job_title, p.person_code))
        conn.executemany("INSERT OR IGNORE INTO org_memberships(person_code,org_code,is_primary) VALUES(?,?,?)", ((m.person_code, m.org_code, int(m.is_primary)) for m in manifest.memberships))
        conn.executemany("INSERT OR IGNORE INTO org_manager_scopes(person_code,org_code) VALUES(?,?)", ((m.person_code, m.org_code) for m in manifest.manager_scopes))
        conn.executemany("INSERT OR IGNORE INTO org_sensitive_qualifications(person_code,sensitivity) VALUES(?,?)", manifest.qualifications)
        for kb in manifest.knowledge_bases:
            assert_kb_available_in_tx(conn, kb.kb_id)
            cur = conn.execute("INSERT OR IGNORE INTO kb_org_policies(kb_id,org_code,kb_type,sensitivity,seed_key) VALUES(?,?,?,?,?)", (kb.kb_id, kb.org_code, kb.kb_type, kb.sensitivity, kb.kb_id))
            created_kbs += cur.rowcount
            conn.execute("INSERT OR IGNORE INTO kb_metadata(kb_id,name,owner_id,scope) VALUES(?,?,0,'private')", (kb.kb_id, kb.name))
        conn.execute("INSERT INTO org_seed_state(seed_key,seed_version,status) VALUES('fictional-org',1,'complete') ON CONFLICT(seed_key) DO UPDATE SET status='complete',updated_at=strftime('%s','now')")
        exact_people = sum(bool(conn.execute(
            "SELECT 1 FROM org_people p JOIN users u ON u.id=p.user_id "
            "WHERE p.person_code=? AND p.seed_key=? AND u.username=?",
            (person.person_code, person.person_code, person.username),
        ).fetchone()) for person in manifest.people)
        exact_kbs = sum(bool(conn.execute(
            "SELECT 1 FROM kb_org_policies WHERE kb_id=? AND seed_key=?",
            (kb.kb_id, kb.kb_id),
        ).fetchone()) for kb in manifest.knowledge_bases)
        if exact_people != len(manifest.people) or exact_kbs != len(manifest.knowledge_bases):
            raise ValueError("seed integrity validation failed")
    if not metadata_only:
        root = documents_root or (Path(db.db_path).resolve().parent / "kb_files")
        for kb in manifest.knowledge_bases:
            for filename, content in kb.documents:
                relative = Path(kb.kb_id) / filename
                destination = root / relative
                with db.locked_read() as conn:
                    existing = conn.execute("SELECT id,status FROM kb_documents WHERE kb_id=? AND filename=?", (kb.kb_id, filename)).fetchone()
                if existing and existing["status"] == "indexed":
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                with db.locked_transaction() as conn:
                    permission = conn.execute("SELECT id FROM document_permissions WHERE kb_id=? AND doc_name=?", (kb.kb_id, filename)).fetchone()
                    if permission is None:
                        permission_id = int(conn.execute("INSERT INTO document_permissions(doc_name,kb_id,owner_id,scope) VALUES(?,?,?,'private')", (filename, kb.kb_id, admin_user_id)).lastrowid)
                    else:
                        permission_id = int(permission[0])
                    conn.execute("INSERT INTO kb_documents(kb_id,filename,file_path,status,document_id) VALUES(?,?,?,'pending',?) ON CONFLICT(kb_id,filename) DO UPDATE SET file_path=excluded.file_path", (kb.kb_id, filename, relative.as_posix(), permission_id))
                if index_document is not None:
                    try:
                        chunk_count = int(index_document(kb.kb_id, str(destination), filename, permission_id))
                    except Exception as exc:
                        with db.locked_transaction() as conn:
                            conn.execute("UPDATE kb_documents SET status='pending',last_error=? WHERE kb_id=? AND filename=?", (str(exc)[:500], kb.kb_id, filename))
                        raise
                    with db.locked_transaction() as conn:
                        conn.execute("UPDATE kb_documents SET status='indexed',chunk_count=?,last_error='' WHERE kb_id=? AND filename=?", (chunk_count, kb.kb_id, filename))
    return SeedReport(created_people, created_units, created_kbs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--index-content", action="store_true")
    args = parser.parse_args()
    db = UserDB(args.db_path)
    try:
        indexer = None
        if args.index_content:
            from qdrant_client.models import Distance, VectorParams

            from config import settings
            from rag.knowledge_base import KnowledgeBaseManager
            manager = KnowledgeBaseManager()
            def indexer(kb_id: str, path: str, filename: str, permission_id: int) -> int:
                if not manager._client.collection_exists(kb_id):
                    manager._client.create_collection(kb_id, vectors_config=VectorParams(size=settings.embed_dimension, distance=Distance.COSINE))
                return manager.add_document(kb_id, path, filename, permission_id)
        report = seed_organization(db, metadata_only=args.metadata_only, index_document=indexer)
        print(f"units={report.created_units} people={report.created_people} knowledge_bases={report.created_knowledge_bases}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
