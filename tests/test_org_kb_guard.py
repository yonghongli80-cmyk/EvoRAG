import pytest
from fastapi import HTTPException

from rag.api_security import require_kb_edit, require_kb_view
from rag.org_auth.repository import OrgRepository
from rag.user_db import UserDB


def test_kb_guard_uses_org_role_not_legacy_owner(tmp_path):
    db = UserDB(str(tmp_path / "guard.db"))
    try:
        member = db.create_user("member", "Password1")
        outsider = db.create_user("outsider", "Password2")
        repo = OrgRepository(db)
        repo.create_unit("TEAM", None, "协作组", "team")
        with db.locked_transaction() as conn:
            conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P1',?,'甲','专员')", (member,))
            conn.execute("INSERT INTO org_memberships VALUES('P1','TEAM',1)")
            conn.execute("INSERT INTO kb_metadata(kb_id,name,owner_id,scope) VALUES('kb','部门库',?,'public')", (outsider,))
            conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('kb','TEAM','organization')")
        assert require_kb_view(db, "kb", db.get_user_by_id(member))["effective_role"] == "read"
        with pytest.raises(HTTPException) as denied:
            require_kb_view(db, "kb", db.get_user_by_id(outsider))
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException):
            require_kb_edit(db, "kb", db.get_user_by_id(member))
    finally:
        db.close()
