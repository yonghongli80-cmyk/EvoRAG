from rag.test_artifact_cleanup import apply_cleanup, find_candidates
from rag.user_db import UserDB


def test_cleanup_selects_only_unambiguous_non_org_test_artifacts(tmp_path):
    db = UserDB(str(tmp_path / "cleanup.db"))
    try:
        test_user = db.create_user("_test_admin", "Password-123!")
        real_user = db.create_user("tester", "Password-123!")
        db.create_kb_metadata("kb_test123", "test_kb_detail", test_user, "private")
        db.create_kb_metadata("kb_team", "测试方法知识库", real_user, "private")
        with db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,kb_type) VALUES('kb_test123','unassigned')"
            )
            conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','测试团队','team')")
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
                "VALUES('kb_team','TEAM','organization')"
            )

        candidates = find_candidates(db)

        assert candidates.user_ids == (test_user,)
        assert candidates.kb_ids == ("kb_test123",)
    finally:
        db.close()


def test_cleanup_apply_revalidates_and_removes_related_rows_atomically(tmp_path):
    db = UserDB(str(tmp_path / "apply.db"))
    try:
        test_user = db.create_user("_perm_owner", "Password-123!")
        db.create_kb_metadata("kb_priv_test", "priv_test", test_user, "private")
        with db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,kb_type) VALUES('kb_priv_test','unassigned')"
            )
            conn.execute(
                "INSERT INTO kb_documents(kb_id,filename,status) "
                "VALUES('kb_priv_test','test.txt','indexed')"
            )

        report = apply_cleanup(db, find_candidates(db))

        assert report == {"users_deleted": 1, "knowledge_bases_deleted": 1}
        assert db.get_user_by_id(test_user) is None
        assert db.get_kb_metadata("kb_priv_test") is None
        with db.locked_read() as conn:
            assert conn.execute(
                "SELECT count(*) FROM kb_org_policies WHERE kb_id='kb_priv_test'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM kb_documents WHERE kb_id='kb_priv_test'"
            ).fetchone()[0] == 0
    finally:
        db.close()
