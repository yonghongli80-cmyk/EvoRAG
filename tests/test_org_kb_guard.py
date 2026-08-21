import pytest
from fastapi import HTTPException

from rag.api_security import (
    require_doc_edit,
    require_doc_view,
    require_kb_administer,
    require_kb_edit,
    require_kb_view,
    require_personal_document_acl,
)
from rag.org_auth.repository import OrgRepository
from rag.org_auth.service import OrgAuthorizationService
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
        with db.locked_read() as conn:
            audit = conn.execute("SELECT action,reason FROM org_audit_events ORDER BY id DESC LIMIT 1").fetchone()
        assert tuple(audit) == ("kb_access_denied", "outside_org_scope")
        with pytest.raises(HTTPException):
            require_kb_edit(db, "kb", db.get_user_by_id(member))
    finally:
        db.close()


def test_kb_documents_inherit_the_knowledge_base_role(tmp_path):
    db = UserDB(str(tmp_path / "documents.db"))
    try:
        member = db.create_user("member", "Password1")
        outsider = db.create_user("outsider", "Password2")
        repo = OrgRepository(db)
        repo.create_unit("TEAM", None, "协作组", "team")
        with db.locked_transaction() as conn:
            conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P1',?,'甲','专员')", (member,))
            conn.execute("INSERT INTO org_memberships VALUES('P1','TEAM',1)")
            conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('kb','TEAM','organization')")
        first = db.create_document_permission("a.txt", "kb", outsider, scope="private")
        second = db.create_document_permission("b.txt", "kb", outsider, scope="private")
        service = OrgAuthorizationService(db)
        assert service.allowed_document_ids(member, "kb") == frozenset({first, second})
        assert service.allowed_document_ids(outsider, "kb") == frozenset()
        assert require_doc_view(db, "a.txt", "kb", db.get_user_by_id(member))["id"] == first
        with pytest.raises(HTTPException) as no_personal_acl_override:
            require_doc_edit(db, "a.txt", "kb", db.get_user_by_id(member))
        assert no_personal_acl_override.value.status_code == 404
        with pytest.raises(HTTPException) as hidden:
            require_doc_view(db, "a.txt", "kb", db.get_user_by_id(outsider))
        assert hidden.value.status_code == 404
    finally:
        db.close()


def test_visible_knowledge_bases_return_org_metadata(tmp_path):
    db = UserDB(str(tmp_path / "list.db"))
    try:
        user = db.create_user("member", "Password1")
        repo = OrgRepository(db)
        repo.create_unit("TEAM", None, "协作组", "team")
        with db.locked_transaction() as conn:
            conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P1',?,'甲','专员')", (user,))
            conn.execute("INSERT INTO org_memberships VALUES('P1','TEAM',1)")
            conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb','协作知识库')")
            conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('kb','TEAM','organization')")
        assert OrgAuthorizationService(db).visible_knowledge_bases(user) == [{
            "kb_id": "kb", "name": "协作知识库", "doc_count": 0,
            "kb_type": "organization", "org_unit": {"code": "TEAM", "name": "协作组"},
            "effective_role": "read", "sensitivity": None,
        }]
    finally:
        db.close()


def test_organization_documents_reject_personal_acl_controls(tmp_path):
    db = UserDB(str(tmp_path / "no-personal-acl.db"))
    try:
        with db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,kb_type) VALUES('kb','company_public')"
            )
        with pytest.raises(HTTPException) as denied:
            require_personal_document_acl(db, "kb")
        assert denied.value.status_code == 404
        require_personal_document_acl(db, "rag_docs")
    finally:
        db.close()


def test_manager_can_create_only_inside_descendant_scope(tmp_path):
    db = UserDB(str(tmp_path / "create.db"))
    try:
        manager = db.create_user("manager", "Password1")
        repo = OrgRepository(db)
        repo.create_units((("DIV", None, "事业群", "division"), ("TEAM", "DIV", "一组", "team"), ("PEER", None, "其他组", "team")))
        with db.locked_transaction() as conn:
            conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P1',?,'甲','负责人')", (manager,))
            conn.execute("INSERT INTO org_manager_scopes VALUES('P1','DIV')")
        service = OrgAuthorizationService(db)
        assert service.can_manage_org(manager, "TEAM") is True
        assert service.can_manage_org(manager, "PEER") is False
        with db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
                "VALUES('team-kb','TEAM','organization')"
            )
        with pytest.raises(HTTPException) as denied:
            require_kb_administer(db, "team-kb", db.get_user_by_id(manager))
        assert denied.value.status_code == 404
    finally:
        db.close()


def test_descendant_members_inherit_ancestor_knowledge_base_read_access(tmp_path):
    db = UserDB(str(tmp_path / "ancestor-read.db"))
    try:
        user_id = db.create_user("member", "Password1")
        repo = OrgRepository(db)
        repo.create_units((
            ("DIV", None, "事业群", "division"),
            ("DEP", "DIV", "部门", "department"),
            ("TEAM", "DEP", "小组", "team"),
        ))
        with db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
                "VALUES('P1',?,'甲','专员')", (user_id,),
            )
            conn.execute("INSERT INTO org_memberships VALUES('P1','TEAM',1)")
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
                "VALUES('division-kb','DIV','organization')"
            )
        decision = OrgAuthorizationService(db).decision(user_id, "division-kb")
        assert decision.allowed is True
        assert decision.role.value == "read"
    finally:
        db.close()
