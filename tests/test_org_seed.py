from rag.org_auth.repository import OrgRepository
from rag.org_auth.seed import seed_organization
from rag.org_auth.seed_manifest import build_seed_manifest
from rag.user_db import UserDB


def test_manifest_has_fixed_counts_and_mappings():
    m = build_seed_manifest()
    assert (len(m.units), len(m.people), len(m.manager_scopes)) == (61, 280, 49)
    assert sum(not row.is_primary for row in m.memberships) == 8
    assert sum(p.is_admin for p in m.people) == 2
    assert len(m.knowledge_bases) == 65
    assert [p.username for p in m.people] == [f"demo{i:03d}" for i in range(1, 281)]
    assert len({p.display_name for p in m.people}) == 280
    assert max(u.depth for u in m.units) == 4
    assert all(1 <= len(k.documents) <= 2 for k in m.knowledge_bases)


def test_seed_is_idempotent_and_preserves_later_edits(tmp_path, monkeypatch):
    monkeypatch.setattr("rag.org_auth.seed.hash_password", lambda value: f"hash:{value}")
    db = UserDB(str(tmp_path / "seed.db"))
    try:
        first = seed_organization(db, metadata_only=True)
        with db.locked_transaction() as conn:
            conn.execute("UPDATE org_people SET display_name='已修改姓名' WHERE person_code='P-001'")
        second = seed_organization(db, metadata_only=True)
        repo = OrgRepository(db)
        assert first.created_people == 280
        assert second.created_people == 0
        assert repo.get_person_for_user(1)["display_name"] == "已修改姓名"
        with db.locked_read() as conn:
            assert conn.execute("SELECT count(*) FROM kb_org_policies").fetchone()[0] == 65
    finally:
        db.close()


def test_seed_quarantines_preexisting_personal_knowledge_bases(tmp_path, monkeypatch):
    monkeypatch.setattr("rag.org_auth.seed.hash_password", lambda value: f"hash:{value}")
    db = UserDB(str(tmp_path / "legacy.db"))
    try:
        owner = db.create_user("legacy", "Password1")
        db.create_kb_metadata("legacy-kb", "旧知识库", owner, "public")
        seed_organization(db, metadata_only=True)
        policy = OrgRepository(db).get_kb_policy("legacy-kb")
        assert policy["kb_type"] == "unassigned"
        assert policy["org_code"] is None
    finally:
        db.close()


def test_seed_documents_are_resumable_and_indexed_once(tmp_path, monkeypatch):
    monkeypatch.setattr("rag.org_auth.seed.hash_password", lambda value: f"hash:{value}")
    calls = []
    def index_document(kb_id, path, filename, permission_id):
        calls.append((kb_id, filename, permission_id))
        return 2
    db = UserDB(str(tmp_path / "content.db"))
    try:
        seed_organization(db, index_document=index_document, documents_root=tmp_path / "docs")
        first_calls = len(calls)
        with db.locked_read() as conn:
            rows = conn.execute("SELECT status,chunk_count FROM kb_documents").fetchall()
        assert first_calls == 100
        assert rows and all(row["status"] == "indexed" and row["chunk_count"] == 2 for row in rows)
        seed_organization(db, index_document=index_document, documents_root=tmp_path / "docs")
        assert len(calls) == first_calls
    finally:
        db.close()
