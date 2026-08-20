from rag.org_auth.migration import migrate_legacy_knowledge_bases
from rag.org_auth.repository import OrgRepository
from rag.user_db import UserDB


def test_legacy_kb_becomes_unassigned_without_touching_file_permissions(tmp_path):
    db = UserDB(str(tmp_path / "migration.db"))
    try:
        owner = db.create_user("owner", "Password1")
        db.create_kb_metadata("legacy", "旧知识库", owner, "public")
        doc_id = db.create_document_permission("private.txt", "rag_docs", owner, scope="private")
        report = migrate_legacy_knowledge_bases(db)
        assert report.migrated == 1
        assert OrgRepository(db).get_kb_policy("legacy")["kb_type"] == "unassigned"
        assert db.get_document_permission_by_id(doc_id)["scope"] == "private"
        assert migrate_legacy_knowledge_bases(db).migrated == 0
    finally:
        db.close()
