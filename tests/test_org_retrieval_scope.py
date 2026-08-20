from rag.org_auth.repository import OrgRepository
from rag.user_db import UserDB
from rag.versioned_retrieval import _visible_document_ids
from rag.permissions import filter_chunks_by_permission
from rag.models import Chunk


def test_org_kb_scope_replaces_personal_document_scope(tmp_path):
    db = UserDB(str(tmp_path / "retrieval.db"))
    try:
        member = db.create_user("member", "Password1")
        owner = db.create_user("owner", "Password2")
        repo = OrgRepository(db)
        repo.create_unit("TEAM", None, "协作组", "team")
        with db.locked_transaction() as conn:
            conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P1',?,'甲','专员')", (member,))
            conn.execute("INSERT INTO org_memberships VALUES('P1','TEAM',1)")
            conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('kb','TEAM','organization')")
        doc_id = db.create_document_permission("secret.txt", "kb", owner, scope="private")
        assert _visible_document_ids(db, db.get_user_by_id(member), "kb") == {doc_id}
        assert _visible_document_ids(db, db.get_user_by_id(owner), "kb") == set()
        chunks = [Chunk(text="组织内容", doc_name="secret.txt", chunk_index=0)]
        assert filter_chunks_by_permission(db, "kb", chunks, db.get_user_by_id(member)) == chunks
        assert filter_chunks_by_permission(db, "kb", chunks, db.get_user_by_id(owner)) == []
    finally:
        db.close()
