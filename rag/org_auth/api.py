from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from rag.api_security import require_user
from rag.org_auth.audit import OrgAuditService
from rag.org_auth.repository import OrgRepository
from rag.org_auth.service import OrgAuthorizationService

router = APIRouter()


def _db(request: Request):
    return request.app.state.user_db


def _company_admin(user: dict) -> None:
    if not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="资源不存在")


@router.get("/login/accounts")
def login_accounts(request: Request, q: str = Query(default="", max_length=50)):
    pattern = f"%{q.strip()}%"
    with _db(request).locked_read() as conn:
        rows = conn.execute("""
            SELECT u.username,p.person_code,p.display_name,p.job_title,o.name AS department
            FROM org_people p JOIN users u ON u.id=p.user_id
            LEFT JOIN org_memberships m ON m.person_code=p.person_code AND m.is_primary=1
            LEFT JOIN org_units o ON o.code=m.org_code
            WHERE p.active=1 AND (u.username LIKE ? OR p.person_code LIKE ? OR p.display_name LIKE ? OR p.job_title LIKE ? OR o.name LIKE ?)
            ORDER BY p.person_code LIMIT 50
        """, (pattern,) * 5).fetchall()
    return [dict(row) for row in rows]


@router.get("/organization/tree")
def organization_tree(request: Request, user: dict = Depends(require_user)):
    db = _db(request)
    with db.locked_read() as conn:
        rows = [dict(r) for r in conn.execute("SELECT code,parent_code,name,kind,active FROM org_units ORDER BY code")]
    if user.get("is_admin"):
        return rows
    person = OrgRepository(db).get_person_for_user(int(user["id"]))
    if person is None:
        return []
    allowed = set()
    repo = OrgRepository(db)
    for scope in repo.manager_scopes(person["person_code"]):
        allowed |= repo.descendants(scope)
    return [row for row in rows if row["code"] in allowed]


@router.get("/organization/units/{code}")
def organization_unit(code: str, request: Request, user: dict = Depends(require_user)):
    visible = organization_tree(request, user)
    if code not in {row["code"] for row in visible}:
        raise HTTPException(status_code=404, detail="资源不存在")
    db = _db(request)
    with db.locked_read() as conn:
        people = [dict(r) for r in conn.execute("""
          SELECT p.person_code,p.display_name,p.job_title,p.active,m.is_primary,u.id AS user_id,u.username
          FROM org_memberships m JOIN org_people p ON p.person_code=m.person_code JOIN users u ON u.id=p.user_id
          WHERE m.org_code=? ORDER BY p.person_code
        """, (code,))]
        kbs = [dict(r) for r in conn.execute("SELECT p.kb_id,m.name,p.kb_type,p.sensitivity FROM kb_org_policies p JOIN kb_metadata m ON m.kb_id=p.kb_id WHERE p.org_code=?", (code,))]
    return {"unit": next(row for row in visible if row["code"] == code), "people": people, "knowledge_bases": kbs}


@router.get("/organization/preview/{user_id}")
def preview_user(user_id: int, request: Request, user: dict = Depends(require_user)):
    _company_admin(user)
    db = _db(request)
    profile = OrgRepository(db).get_profile_for_user(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    return {"user_id": user_id, "profile": profile.__dict__, "knowledge_bases": OrgAuthorizationService(db).visible_knowledge_bases(user_id)}


class PersonUpdate(BaseModel):
    active: bool | None = None
    job_title: str | None = None
    primary_org_code: str | None = None
    secondary_org_codes: list[str] | None = None
    manager_org_codes: list[str] | None = None
    sensitivities: list[str] | None = None


@router.patch("/organization/people/{person_code}")
def update_person(person_code: str, body: PersonUpdate, request: Request, user: dict = Depends(require_user)):
    _company_admin(user)
    db = _db(request)
    with db.locked_transaction(immediate=True) as conn:
        row = conn.execute("SELECT user_id FROM org_people WHERE person_code=?", (person_code,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        if body.active is not None:
            conn.execute("UPDATE org_people SET active=?,security_version=security_version+1 WHERE person_code=?", (int(body.active), person_code))
        if body.job_title is not None:
            conn.execute("UPDATE org_people SET job_title=? WHERE person_code=?", (body.job_title.strip(), person_code))
        if body.primary_org_code is not None or body.secondary_org_codes is not None:
            current_primary = body.primary_org_code or conn.execute("SELECT org_code FROM org_memberships WHERE person_code=? AND is_primary=1", (person_code,)).fetchone()[0]
            secondaries = body.secondary_org_codes or []
            conn.execute("DELETE FROM org_memberships WHERE person_code=?", (person_code,))
            conn.execute("INSERT INTO org_memberships VALUES(?,?,1)", (person_code, current_primary))
            conn.executemany("INSERT INTO org_memberships VALUES(?,?,0)", ((person_code, code) for code in secondaries if code != current_primary))
            conn.execute("UPDATE org_people SET security_version=security_version+1 WHERE person_code=?", (person_code,))
        if body.manager_org_codes is not None:
            conn.execute("DELETE FROM org_manager_scopes WHERE person_code=?", (person_code,))
            conn.executemany("INSERT INTO org_manager_scopes VALUES(?,?)", ((person_code, code) for code in body.manager_org_codes))
        if body.sensitivities is not None:
            conn.execute("DELETE FROM org_sensitive_qualifications WHERE person_code=?", (person_code,))
            conn.executemany("INSERT INTO org_sensitive_qualifications VALUES(?,?)", ((person_code, value) for value in body.sensitivities))
    OrgAuditService(db).record(int(user["id"]), "person_updated", "person", person_code, "success", "admin_update")
    return {"status": "updated"}


class GrantRequest(BaseModel):
    kb_id: str
    subject_type: str
    subject_id: str
    role: str
    effect: str


@router.post("/organization/grants")
def create_grant(body: GrantRequest, request: Request, user: dict = Depends(require_user)):
    _company_admin(user)
    if body.subject_type not in {"user", "org"} or body.role not in {"read", "manage"} or body.effect not in {"allow", "deny"}:
        raise HTTPException(status_code=400, detail="授权参数无效")
    db = _db(request)
    with db.locked_transaction() as conn:
        cur = conn.execute("INSERT INTO kb_org_grants(kb_id,subject_type,subject_id,role,effect,granted_by) VALUES(?,?,?,?,?,?)", (body.kb_id, body.subject_type, body.subject_id, body.role, body.effect, user["id"]))
    OrgAuditService(db).record(int(user["id"]), "grant_created", "knowledge_base", body.kb_id, "success", body.effect)
    return {"id": int(cur.lastrowid)}


@router.get("/organization/audit")
def audit_events(request: Request, user: dict = Depends(require_user)):
    _company_admin(user)
    return OrgAuditService(_db(request)).list_events()
