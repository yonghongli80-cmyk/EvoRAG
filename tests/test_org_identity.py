from rag.api_security import resolve_principal
from rag.auth import create_token
from rag.org_auth.repository import OrgRepository
from rag.user_db import UserDB


def _identity_db(tmp_path):
    db = UserDB(str(tmp_path / "identity.db"))
    user_id = db.create_user("fixed001", "Password1")
    repo = OrgRepository(db)
    repo.install_schema()
    repo.create_unit("ROOT", None, "星河集团", "company")
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P-001',?,?,?)", (user_id, "林知远", "负责人"))
        conn.execute("INSERT INTO org_memberships(person_code,org_code,is_primary) VALUES('P-001','ROOT',1)")
    return db, user_id, repo


def test_inactive_identity_and_old_security_version_are_rejected(tmp_path):
    db, user_id, _ = _identity_db(tmp_path)
    try:
        valid = create_token({"user_id": user_id, "username": "fixed001", "security_version": 1})
        assert resolve_principal(db, f"Bearer {valid}", None) is not None
        with db.locked_transaction() as conn:
            conn.execute("UPDATE org_people SET active=0,security_version=2 WHERE user_id=?", (user_id,))
        assert resolve_principal(db, f"Bearer {valid}", None) is None
    finally:
        db.close()


def test_profile_uses_stable_person_mapping(tmp_path):
    db, user_id, repo = _identity_db(tmp_path)
    try:
        profile = repo.get_profile_for_user(user_id)
        assert profile.person_code == "P-001"
        assert profile.display_name == "林知远"
        assert profile.primary_org == {"code": "ROOT", "name": "星河集团"}
    finally:
        db.close()
