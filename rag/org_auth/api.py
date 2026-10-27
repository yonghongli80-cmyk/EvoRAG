from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

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


class KnowledgeBasePolicyRequest(BaseModel):
    """Assign a legacy/unassigned KB to an organization policy."""

    org_code: str = Field(min_length=1, max_length=64)
    kb_type: Literal["organization", "company_public", "sensitive"] = "organization"
    sensitivity: str | None = Field(default=None, max_length=64)


@router.put("/organization/knowledge-bases/{kb_id}/policy")
def assign_knowledge_base_policy(
    kb_id: str,
    body: KnowledgeBasePolicyRequest,
    request: Request,
    user: dict = Depends(require_user),
):
    """Move an unassigned knowledge base under an explicit organization policy.

    This is the administrative step the 2026-08-20 migration defers: legacy KBs
    stay owner/admin-only until an admin assigns them. Reusing the existing
    ``kb_org_policies`` table keeps reads/writes on the same authorization path.
    """
    _company_admin(user)
    db = _db(request)
    with db.locked_transaction() as conn:
        if conn.execute("SELECT 1 FROM kb_metadata WHERE kb_id=?", (kb_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        if (
            conn.execute(
                "SELECT 1 FROM org_units WHERE code=? AND active=1", (body.org_code,)
            ).fetchone()
            is None
        ):
            raise HTTPException(status_code=404, detail="组织不存在")
        if body.kb_type == "sensitive" and not (body.sensitivity or "").strip():
            raise HTTPException(status_code=400, detail="敏感库必须指定敏感域")
        existing = conn.execute(
            "SELECT 1 FROM kb_org_policies WHERE kb_id=?", (kb_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,org_code,kb_type,sensitivity,active) "
                "VALUES(?,?,?,?,1)",
                (kb_id, body.org_code, body.kb_type, body.sensitivity),
            )
        else:
            conn.execute(
                "UPDATE kb_org_policies SET org_code=?, kb_type=?, sensitivity=?, active=1 "
                "WHERE kb_id=?",
                (body.org_code, body.kb_type, body.sensitivity, kb_id),
            )
        OrgAuditService(db).record(
            int(user["id"]),
            "kb_policy_assigned",
            "knowledge_base",
            kb_id,
            "success",
            body.org_code,
            {"kb_type": body.kb_type, "sensitivity": body.sensitivity},
        )
    return {
        "kb_id": kb_id,
        "org_code": body.org_code,
        "kb_type": body.kb_type,
        "sensitivity": body.sensitivity,
    }



@router.get("/login/accounts")
def login_accounts(
    request: Request,
    q: str = Query(default="", max_length=50),
    user: dict = Depends(require_user),
):
    _company_admin(user)
    term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{term}%"
    with _db(request).locked_read() as conn:
        rows = conn.execute("""
            SELECT u.username,p.person_code,p.display_name,p.job_title,o.name AS department
            FROM org_people p JOIN users u ON u.id=p.user_id
            LEFT JOIN org_memberships m ON m.person_code=p.person_code AND m.is_primary=1
            LEFT JOIN org_units o ON o.code=m.org_code
            WHERE p.active=1 AND (u.username LIKE ? ESCAPE '\\' OR p.person_code LIKE ? ESCAPE '\\' OR p.display_name LIKE ? ESCAPE '\\' OR p.job_title LIKE ? ESCAPE '\\' OR o.name LIKE ? ESCAPE '\\')
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
        for person in people:
            person["primary_org_code"] = next((r[0] for r in conn.execute(
                "SELECT org_code FROM org_memberships WHERE person_code=? AND is_primary=1",
                (person["person_code"],),
            )), None)
            person["secondary_org_codes"] = [r[0] for r in conn.execute(
                "SELECT org_code FROM org_memberships WHERE person_code=? AND is_primary=0 ORDER BY org_code",
                (person["person_code"],),
            )]
            person["manager_org_codes"] = [r[0] for r in conn.execute(
                "SELECT org_code FROM org_manager_scopes WHERE person_code=? ORDER BY org_code",
                (person["person_code"],),
            )]
            person["sensitivities"] = [r[0] for r in conn.execute(
                "SELECT sensitivity FROM org_sensitive_qualifications WHERE person_code=? ORDER BY sensitivity",
                (person["person_code"],),
            )]
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


_SENSITIVITY_DOMAINS = frozenset({"finance", "hr", "customer"})


def _validate_org_codes(conn, codes: list[str] | None) -> None:
    if codes is None:
        return
    wanted = {str(code).strip() for code in codes if str(code).strip()}
    if not wanted:
        return
    placeholders = ",".join("?" for _ in wanted)
    found = {
        row[0]
        for row in conn.execute(
            f"SELECT code FROM org_units WHERE active=1 AND code IN ({placeholders})",
            tuple(wanted),
        )
    }
    missing = wanted - found
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"无效组织编号: {', '.join(sorted(missing))}",
        )


@router.patch("/organization/people/{person_code}")
def update_person(person_code: str, body: PersonUpdate, request: Request, user: dict = Depends(require_user)):
    _company_admin(user)
    db = _db(request)
    changed = [
        name
        for name in ("active", "job_title", "primary_org_code", "secondary_org_codes",
                     "manager_org_codes", "sensitivities")
        if getattr(body, name) is not None
    ]
    if body.sensitivities is not None and not set(body.sensitivities).issubset(_SENSITIVITY_DOMAINS):
        raise HTTPException(status_code=400, detail="无效敏感域")
    with db.locked_transaction(immediate=True) as conn:
        row = conn.execute("SELECT user_id FROM org_people WHERE person_code=?", (person_code,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        _validate_org_codes(
            conn, [body.primary_org_code] if body.primary_org_code else None
        )
        _validate_org_codes(conn, body.secondary_org_codes)
        _validate_org_codes(conn, body.manager_org_codes)
        if body.active is not None:
            conn.execute("UPDATE org_people SET active=?,security_version=security_version+1 WHERE person_code=?", (int(body.active), person_code))
        if body.job_title is not None:
            conn.execute("UPDATE org_people SET job_title=? WHERE person_code=?", (body.job_title.strip(), person_code))
        if body.primary_org_code is not None or body.secondary_org_codes is not None:
            primary_row = conn.execute(
                "SELECT org_code FROM org_memberships "
                "WHERE person_code=? AND is_primary=1",
                (person_code,),
            ).fetchone()
            current_primary = body.primary_org_code or (primary_row[0] if primary_row else None)
            if current_primary is None:
                raise HTTPException(status_code=400, detail="必须指定主部门")
            if body.secondary_org_codes is None:
                secondaries = [
                    membership[0] for membership in conn.execute(
                        "SELECT org_code FROM org_memberships "
                        "WHERE person_code=? AND is_primary=0 ORDER BY org_code",
                        (person_code,),
                    )
                ]
            else:
                secondaries = body.secondary_org_codes
            conn.execute("DELETE FROM org_memberships WHERE person_code=?", (person_code,))
            conn.execute("INSERT INTO org_memberships VALUES(?,?,1)", (person_code, current_primary))
            conn.executemany("INSERT INTO org_memberships VALUES(?,?,0)", ((person_code, code) for code in secondaries if code != current_primary))
            conn.execute("UPDATE org_people SET security_version=security_version+1 WHERE person_code=?", (person_code,))
        if body.manager_org_codes is not None:
            conn.execute("DELETE FROM org_manager_scopes WHERE person_code=?", (person_code,))
            conn.executemany("INSERT INTO org_manager_scopes VALUES(?,?)", ((person_code, code) for code in body.manager_org_codes))
            conn.execute("UPDATE org_people SET security_version=security_version+1 WHERE person_code=?", (person_code,))
        if body.sensitivities is not None:
            conn.execute("DELETE FROM org_sensitive_qualifications WHERE person_code=?", (person_code,))
            conn.executemany("INSERT INTO org_sensitive_qualifications VALUES(?,?)", ((person_code, value) for value in body.sensitivities))
            conn.execute("UPDATE org_people SET security_version=security_version+1 WHERE person_code=?", (person_code,))
        OrgAuditService(db).record(
            int(user["id"]),
            "person_updated",
            "person",
            person_code,
            "success",
            "admin_update",
            {"changed": changed},
        )
    return {"status": "updated"}


class GrantRequest(BaseModel):
    kb_id: str
    subject_type: str
    subject_id: str
    role: str
    effect: str


def _invalidate_grant_subject(conn, subject_type: str, subject_id: str) -> None:
    if subject_type == "user":
        conn.execute(
            "UPDATE org_people SET security_version=security_version+1 WHERE user_id=?",
            (int(subject_id),),
        )
        return
    conn.execute("""
        WITH RECURSIVE tree(code) AS (
          SELECT code FROM org_units WHERE code=?
          UNION ALL SELECT u.code FROM org_units u JOIN tree t ON u.parent_code=t.code
        )
        UPDATE org_people SET security_version=security_version+1
        WHERE person_code IN (
          SELECT DISTINCT m.person_code FROM org_memberships m JOIN tree t ON t.code=m.org_code
        )
    """, (subject_id,))


@router.post("/organization/grants")
def create_grant(body: GrantRequest, request: Request, user: dict = Depends(require_user)):
    _company_admin(user)
    if body.subject_type not in {"user", "org"} or body.role not in {"read", "manage"} or body.effect not in {"allow", "deny"}:
        raise HTTPException(status_code=400, detail="授权参数无效")
    db = _db(request)
    with db.locked_transaction() as conn:
        kb = conn.execute(
            "SELECT 1 FROM kb_org_policies WHERE kb_id=? AND active=1", (body.kb_id,)
        ).fetchone()
        if kb is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        if body.subject_type == "user":
            try:
                subject_user_id = int(body.subject_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="授权参数无效") from exc
            subject = conn.execute(
                "SELECT person_code FROM org_people WHERE user_id=? AND active=1",
                (subject_user_id,),
            ).fetchone()
        else:
            subject = conn.execute(
                "SELECT code FROM org_units WHERE code=? AND active=1", (body.subject_id,)
            ).fetchone()
        if subject is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        cur = conn.execute("INSERT INTO kb_org_grants(kb_id,subject_type,subject_id,role,effect,granted_by) VALUES(?,?,?,?,?,?)", (body.kb_id, body.subject_type, body.subject_id, body.role, body.effect, user["id"]))
        _invalidate_grant_subject(conn, body.subject_type, body.subject_id)
        OrgAuditService(db).record(int(user["id"]), "grant_created", "knowledge_base", body.kb_id, "success", body.effect)
    return {"id": int(cur.lastrowid)}


@router.delete("/organization/grants/{grant_id}")
def revoke_grant(grant_id: int, request: Request, user: dict = Depends(require_user)):
    _company_admin(user)
    db = _db(request)
    with db.locked_transaction() as conn:
        grant = conn.execute(
            "SELECT * FROM kb_org_grants WHERE id=? AND active=1", (grant_id,)
        ).fetchone()
        if grant is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        conn.execute("UPDATE kb_org_grants SET active=0 WHERE id=?", (grant_id,))
        _invalidate_grant_subject(conn, grant["subject_type"], grant["subject_id"])
        OrgAuditService(db).record(
            int(user["id"]), "grant_revoked", "knowledge_base", grant["kb_id"],
            "success", "admin_revoke",
        )
    return {"status": "revoked", "id": grant_id}


@router.get("/organization/audit")
def audit_events(request: Request, user: dict = Depends(require_user)):
    _company_admin(user)
    return OrgAuditService(_db(request)).list_events()
