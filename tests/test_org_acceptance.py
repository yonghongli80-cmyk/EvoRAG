from rag.auth import hash_password
from rag.org_auth.seed import seed_organization
from rag.org_auth.service import OrgAuthorizationService
from rag.user_db import UserDB


def test_fixed_accounts_and_knowledge_base_visibility_acceptance(tmp_path, monkeypatch):
    initial_hash = hash_password("123456")
    monkeypatch.setattr("rag.org_auth.seed.hash_password", lambda _value: initial_hash)
    db = UserDB(str(tmp_path / "acceptance.db"))
    try:
        seed_organization(db, documents_root=tmp_path / "documents")
        seed_organization(db, documents_root=tmp_path / "documents")

        with db.locked_read() as conn:
            assert conn.execute("SELECT count(*) FROM org_people").fetchone()[0] == 280
            assert conn.execute("SELECT count(*) FROM org_units").fetchone()[0] == 61
            assert conn.execute("SELECT count(*) FROM kb_org_policies").fetchone()[0] == 65
            assert conn.execute("SELECT count(*) FROM kb_documents").fetchone()[0] == 100
            assert conn.execute("SELECT count(*) FROM org_manager_scopes").fetchone()[0] == 49
            assert conn.execute(
                "SELECT count(*) FROM org_memberships WHERE is_primary=0"
            ).fetchone()[0] == 8

        # Every fixed account maps to one active person and accepts the approved password.
        for number in range(1, 281):
            username = f"demo{number:03d}"
            user = db.authenticate(username, "123456")
            assert user is not None and user["username"] == username

        service = OrgAuthorizationService(db)
        admin = db.get_user_by_username("demo001")
        manager = db.get_user_by_username("demo003")
        qualified_employee = db.get_user_by_username("demo050")
        employee = db.get_user_by_username("demo280")
        assert len(service.allowed_kb_ids(admin["id"])) == 65
        assert any(
            service.decision(manager["id"], kb_id).role.value == "manage"
            for kb_id in service.allowed_kb_ids(manager["id"])
        )
        assert "kb_sensitive_customer" in service.allowed_kb_ids(qualified_employee["id"])
        assert not any(
            kb_id.startswith("kb_sensitive_")
            for kb_id in service.allowed_kb_ids(employee["id"])
        )
        assert "kb_company_public" in service.allowed_kb_ids(employee["id"])
    finally:
        db.close()


def test_file_page_permission_model_is_not_rewritten_by_org_seed(tmp_path, monkeypatch):
    monkeypatch.setattr("rag.org_auth.seed.hash_password", lambda value: f"hash:{value}")
    db = UserDB(str(tmp_path / "files.db"))
    try:
        owner = db.create_user("file-owner", "Password1")
        document_id = db.create_document_permission(
            "personal.md", "rag_docs", owner, scope="shared"
        )
        seed_organization(db, metadata_only=True)
        permission = db.get_document_permission_by_id(document_id)
        assert permission["scope"] == "shared"
        assert permission["owner_id"] == owner
    finally:
        db.close()
